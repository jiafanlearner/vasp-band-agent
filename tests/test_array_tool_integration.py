"""Array model tools and local/remote submission boundaries; mock traffic only."""
import json
from copy import deepcopy
from types import SimpleNamespace as NS

import pytest
from jsonschema import ValidationError, validate

from vasp_agent.chat import ai_agent as ai
from vasp_agent.permissions.resource_menu import preference_path, resource_menu, save_defaults
from vasp_agent.remote import remote_worker as worker
from test_remote_array import ARRAY_LIMITS, ARRAY_RESOURCES, task as remote_task
from vasp_agent.common import agent_paths


RESOURCES = dict(nodes=1, parallel_tests=3, cores_per_test=16,
                 partition='compute', walltime='04:00:00')
ARRAY = {**RESOURCES, 'submission_mode': 'array'}
LIMITS = dict(nodes=3, cores=48, partition='compute', walltime='04:00:00')


def schema_for(name):
    return next(tool for tool in ai.TOOLS if tool['name'] == name)


def assert_strict_objects(schema):
    if schema.get('type') == 'object':
        assert schema.get('additionalProperties') is False
        assert set(schema['required']) == set(schema['properties'])
        for child in schema['properties'].values():
            assert_strict_objects(child)
    for keyword in ('anyOf', 'oneOf', 'allOf'):
        for child in schema.get(keyword, []):
            assert_strict_objects(child)
    if isinstance(schema.get('items'), dict):
        assert_strict_objects(schema['items'])


@pytest.mark.parametrize('name', ['apply_submission_resources', 'recommend_submission_resources'])
def test_model_resource_tools_keep_every_nested_object_strict(name):
    tool = schema_for(name)
    assert tool['strict'] is True
    assert_strict_objects(tool['parameters'])


@pytest.mark.parametrize('resources', [RESOURCES, ARRAY])
def test_apply_tool_accepts_legacy_and_explicit_mode_resources(tmp_path, monkeypatch, resources):
    box = ai.Toolbox(tmp_path)
    received = []
    monkeypatch.setattr(box, 'apply_submission_resources',
                        lambda **kwargs: received.append(kwargs) or {'applied': True, 'submitted': False})
    arguments = dict(server='test', job_name='si', phase='convergence', resources=deepcopy(resources))
    validate(arguments, schema_for('apply_submission_resources')['parameters'])
    result = box.call('apply_submission_resources', arguments)
    assert result['ok'] and result['result']['submitted'] is False
    assert received == [arguments]
    assert not box.allow_submit and box.submissions == 0


def test_apply_tool_treats_null_mode_as_omitted(tmp_path, monkeypatch):
    # The advertised (terse) schema makes submission_mode a nullable required
    # field; null reaches the backend exactly as an omitted mode (allocation).
    box = ai.Toolbox(tmp_path)
    received = []
    monkeypatch.setattr(box, 'apply_submission_resources',
                        lambda **kwargs: received.append(kwargs) or {'applied': True, 'submitted': False})
    arguments = dict(server='test', job_name='si', phase='convergence', resources={**RESOURCES, 'submission_mode': None})
    assert box.call('apply_submission_resources', arguments)['ok']
    assert received == [{**arguments, 'resources': RESOURCES}]


@pytest.mark.parametrize('resources', [
    {**ARRAY, 'submission_mode': 'batch'},
    {**ARRAY, 'submission_mode': 1},
    {**ARRAY, 'unexpected': True},
    {key: value for key, value in ARRAY.items() if key != 'nodes'},
])
def test_apply_tool_rejects_invalid_resource_shape_before_backend(tmp_path, monkeypatch, resources):
    box = ai.Toolbox(tmp_path)
    monkeypatch.setattr(box, 'apply_submission_resources', lambda **kwargs: pytest.fail('invalid tool input reached backend'))
    arguments = dict(server='test', job_name='si', phase='convergence', resources=resources)
    with pytest.raises(ValidationError):
        validate(arguments, schema_for('apply_submission_resources')['parameters'])
    assert not box.call('apply_submission_resources', arguments)['ok']
    assert not box.allow_submit


def recommend_args(**changes):
    return dict(server='test', job_name='si', phase='convergence', signature='snapshot',
                parallel_tests=3, cores_per_test=16, nodes=1,
                rationale='每个扫描独立申请一个节点，最大三项并发。',
                submission_mode='array', **changes)


def recommendation_box(tmp_path, monkeypatch):
    box = ai.Toolbox(tmp_path)
    box.submission_limits = {**LIMITS, 'server': 'test'}
    calls = []
    monkeypatch.setattr(box, 'get_submission_resources', lambda server, job_name, phase:
                        calls.append((server, job_name, phase)) or
                        dict(signature='snapshot', resources=deepcopy(RESOURCES), execution_plan={'tests': 6}))
    monkeypatch.setattr(ai.subprocess, 'run', lambda *args, **kwargs: pytest.fail('a recommendation must not launch or submit'))
    return box, calls


def test_array_recommendation_is_schema_valid_and_saved_without_submission(tmp_path, monkeypatch):
    box, calls = recommendation_box(tmp_path, monkeypatch)
    result = box.call('recommend_submission_resources', recommend_args())
    assert result['ok']
    suggestion = result['result']
    assert suggestion['saved'] and suggestion['operator_confirmed'] is False
    assert suggestion['resources'] == ARRAY
    assert box.resource_suggestions[('test', 'si', 'convergence')]['resources'] == ARRAY
    assert calls == [('test', 'si', 'convergence')]
    assert not box.allow_submit and box.submissions == 0 and box.submission_target is None
    assert not (tmp_path / 'runs').exists()


def test_recommendation_nullable_mode_is_required_on_wire_and_legacy_calls_still_work(tmp_path, monkeypatch):
    schema = schema_for('recommend_submission_resources')['parameters']
    assert 'submission_mode' in schema['required']
    arguments = recommend_args()
    for mode in (None, 'allocation', 'array'):
        validate({**arguments, 'submission_mode': mode}, schema)
    legacy = {key: value for key, value in arguments.items() if key != 'submission_mode'}
    with pytest.raises(ValidationError):
        validate(legacy, schema)
    box, _ = recommendation_box(tmp_path, monkeypatch)
    result = box.call('recommend_submission_resources', legacy)
    assert result['ok']
    assert result['result']['resources'].get('submission_mode', 'allocation') == 'allocation'
    assert not box.allow_submit and box.submissions == 0


@pytest.mark.parametrize('phase', ['relaxation', 'scf', 'bands'])
def test_array_recommendation_for_later_stage_is_rejected_without_saving(tmp_path, monkeypatch, phase):
    box, _ = recommendation_box(tmp_path, monkeypatch)
    arguments = {**recommend_args(), 'phase': phase, 'parallel_tests': 1}
    assert not box.call('recommend_submission_resources', arguments)['ok']
    assert not box.resource_suggestions and not box.allow_submit


@pytest.mark.parametrize('answer', ['array', '模式=array', 'submission_mode=array'])
def test_local_menu_rejects_array_input_and_can_confirm_allocation(tmp_path, answer):
    responses = iter([answer, '1'])
    output, prompts = [], []
    def read(prompt):
        prompts.append(prompt)
        return next(responses)
    selected = resource_menu(tmp_path, 'local', 'convergence', RESOURCES,
                             read, output.append, job='si')
    assert selected.get('submission_mode', 'allocation') == 'allocation'
    assert {key: selected[key] for key in RESOURCES} == RESOURCES
    assert len(prompts) == 2
    assert not agent_paths.store_dir(tmp_path, 'submission_defaults').exists()


@pytest.mark.parametrize('source', ['saved', 'current'])
def test_local_menu_does_not_adopt_old_array_defaults(tmp_path, source):
    current = ARRAY if source == 'current' else RESOURCES
    responses = iter(['1', 'allocation', '1'] if source == 'current' else ['1'])
    path = preference_path(tmp_path, 'local', 'convergence')
    if source == 'saved':
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(ARRAY), encoding='utf-8')
        original = path.read_bytes()
    selected = resource_menu(tmp_path, 'local', 'convergence', current,
                             lambda _: next(responses), lambda _: None, job='si')
    assert selected.get('submission_mode', 'allocation') == 'allocation'
    assert {key: selected[key] for key in RESOURCES} == RESOURCES
    if source == 'saved':
        assert path.read_bytes() == original
    else:
        assert not path.exists()


def test_local_array_defaults_cannot_be_written(tmp_path):
    with pytest.raises(ValueError):
        save_defaults(tmp_path, 'local', 'convergence', ARRAY)
    assert not agent_paths.store_dir(tmp_path, 'submission_defaults').exists()


@pytest.mark.parametrize('array_source', ['config', 'configured_script', 'task_script'])
def test_local_submit_backend_refuses_array_before_sbatch_or_slot_consumption(tmp_path, monkeypatch, array_source):
    folder = tmp_path / 'runs/si'
    folder.mkdir(parents=True)
    config = {**RESOURCES, 'submission_mode': 'array' if array_source == 'config' else 'allocation'}
    (folder / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    script = '#!/bin/bash\n#SBATCH --partition=compute\n#SBATCH --nodes=1\n#SBATCH --ntasks=16\n#SBATCH --time=04:00:00\n'
    configured = tmp_path / 'job.slurm'
    configured.write_text(script + ('#SBATCH --array=0-5%3\n' if array_source == 'configured_script' else ''), encoding='utf-8')
    if array_source == 'task_script':
        (folder / 'submit.slurm').write_text(script + '#SBATCH --array=0-5%3\n', encoding='utf-8')
    box = ai.Toolbox(tmp_path, submit_script=configured, allow_submit=True)
    monkeypatch.setattr(ai.core, 'verify', lambda _: {'workflow': 'full'})
    monkeypatch.setattr('vasp_agent.permissions.submission_dialog.confirm', lambda *args, **kwargs: None)
    launched = []
    monkeypatch.setattr(ai.subprocess, 'run', lambda *args, **kwargs: launched.append(args) or NS(stdout='4500\n'))
    result = box.call('submit_job', {'job_name': 'si'})
    assert not result['ok']
    assert launched == []
    assert box.allow_submit and box.submissions == 0
    assert not (folder / 'submission.json').exists() and not (folder / 'submissions').exists()


@pytest.fixture
def remote_array_preflight(remote_task, monkeypatch):
    folder, modules = remote_task
    before = worker.resource_context(folder, 'convergence')
    worker.configure_resources(folder, 'convergence', ARRAY_RESOURCES,
                               before['signature'], ARRAY_LIMITS)
    monkeypatch.setattr(worker, 'inspect_environment', lambda *args: {
        'executable_sha256': 'synthetic', 'environment_sha256': 'synthetic', 'executable': '/mock/vasp'})
    capacity_calls = []
    monkeypatch.setattr(worker, 'capacity_preflight',
                        lambda script: capacity_calls.append(script) or {'capacity_checked': True})
    return folder, modules, capacity_calls


@pytest.mark.parametrize('change', ['config_allocation', 'missing_array', 'task_count',
                                   'concurrency', 'cores', 'nodes', 'launcher'])
def test_remote_preflight_rejects_config_script_array_mismatch_before_capacity(remote_array_preflight, change):
    folder, _, capacity_calls = remote_array_preflight
    config = json.loads((folder / 'config.json').read_text(encoding='utf-8'))
    script = (folder / 'submit.slurm').read_text(encoding='utf-8')
    if change == 'config_allocation':
        config['submission_mode'] = 'allocation'
    elif change == 'missing_array':
        script = script.replace('#SBATCH --array=0-5%3\n', '')
    else:
        replacements = {
            'task_count': ('--array=0-5%3', '--array=0-4%3'),
            'concurrency': ('--array=0-5%3', '--array=0-5%2'),
            'cores': ('--ntasks=16', '--ntasks=18'),
            'nodes': ('--nodes=2', '--nodes=1'),
            'launcher': ('run-array-test', 'run'),
        }
        old, new = replacements[change]
        assert old in script
        script = script.replace(old, new)
    (folder / 'config.json').write_text(json.dumps(config), encoding='utf-8')
    (folder / 'submit.slurm').write_text(script, encoding='utf-8')
    metadata = json.loads((folder / 'remote-job.json').read_text(encoding='utf-8'))
    metadata['script_sha256'] = worker.sha(folder / 'submit.slurm')
    (folder / 'remote-job.json').write_text(json.dumps(metadata), encoding='utf-8')
    with pytest.raises(ValueError):
        worker.dispatch(dict(config={'root': str(folder.parents[1])}, action='submission_preflight',
                             job_name=folder.name, phase='convergence'))
    assert capacity_calls == []
    assert not (folder / 'submissions').exists() and not (folder / 'remote-submission.json').exists()


def test_remote_array_preflight_checks_capacity_without_sbatch(remote_array_preflight):
    folder, _, capacity_calls = remote_array_preflight
    result = worker.dispatch(dict(config={'root': str(folder.parents[1])}, action='submission_preflight',
                                  job_name=folder.name, phase='convergence'))
    assert result['ready'] and len(capacity_calls) == 1
    assert '#SBATCH --array=0-5%3' in capacity_calls[0]
    assert not (folder / 'submissions').exists() and not (folder / 'remote-submission.json').exists()
