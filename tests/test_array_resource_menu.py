"""Array previews and grants use only mock transport and operator input."""
import json
from types import SimpleNamespace as NS

import pytest

from vasp_agent.permissions.resource_menu import load_defaults, parse_custom, save_defaults
from vasp_agent.permissions.submission_dialog import confirm, same_resources, validate_resources
from test_resource_menu import RES, menu
from test_submission_permissions import commands
from vasp_agent.common import agent_paths

ARRAY = {**RES, 'nodes': 1, 'submission_mode': 'array'}
PEAK_LIMITS = dict(nodes=6, cores=96, partition='compute', walltime='04:00:00')


@pytest.mark.parametrize('mode_field', ['模式', 'submission_mode'])
def test_array_custom_preview_shows_per_element_and_concurrent_peaks(tmp_path, mode_field):
    selected, output, prompts = menu(tmp_path, [f'{mode_field}=array 节点=1', '1'], job='si')
    assert selected == ARRAY and len(prompts) == 2
    shown = '\n'.join(output)
    assert '16 MPI × 1 节点' in shown and '每节点 16 MPI' in shown
    assert '最多同时运行  6 个' in shown
    assert '96 核 / 6 节点' in shown
    assert '共 6 项测试分别排队' in shown and '可能部分先开始运行' in shown
    assert 'squeue -r' in shown
    assert not agent_paths.store_dir(tmp_path, 'submission_defaults').exists()


def test_quick_mode_switch_requires_a_separate_confirmation(tmp_path):
    selected, output, prompts = menu(tmp_path, ['array', 'allocation', '1'],
                                      job='si', current={**RES, 'nodes': 1})
    assert selected == {**RES, 'nodes': 1, 'submission_mode': 'allocation'}
    assert len(prompts) == 3
    assert sum('操作者切换模式，待确认' in text for text in output) == 2
    assert not agent_paths.store_dir(tmp_path, 'submission_defaults').exists()


def test_saved_array_concurrency_shrinks_to_remaining_tests(tmp_path):
    save_defaults(tmp_path, 'test', 'convergence', ARRAY)
    selected, output, prompts = menu(tmp_path, ['1'], tests=2, job='si')
    assert selected == {**ARRAY, 'parallel_tests': 2} and len(prompts) == 1
    assert any('32 核 / 2 节点' in text for text in output)
    assert load_defaults(tmp_path, 'test', 'convergence') == ARRAY


def test_array_node_edit_reprompts_only_invalid_peak(tmp_path):
    selected, output, prompts = menu(tmp_path, ['3', '', '', '2', '1', '', '', '1'],
                                      current=ARRAY, job='si', limits=PEAK_LIMITS)
    assert selected == ARRAY
    assert sum(prompt.startswith('每个数组元素申请') for prompt in prompts) == 2
    assert any('节点峰值为并发数 × 每项节点数' in text for text in output)
    assert not agent_paths.store_dir(tmp_path, 'submission_defaults').exists()


def test_array_defaults_round_trip_without_grant_and_legacy_five_fields_still_load(tmp_path):
    save_defaults(tmp_path, 'test', 'convergence', ARRAY)
    assert load_defaults(tmp_path, 'test', 'convergence') == ARRAY
    path = agent_paths.store_dir(tmp_path, 'submission_defaults') / 'test-scan.json'
    assert set(json.loads(path.read_text())) == set(ARRAY)
    path.write_text(json.dumps(RES), encoding='utf-8')
    assert load_defaults(tmp_path, 'test', 'convergence') == RES
    assert same_resources(RES, {**RES, 'submission_mode': 'allocation'})
    assert not same_resources(RES, {**RES, 'submission_mode': 'array'})


@pytest.mark.parametrize('phase', [None, 'relaxation', 'scf', 'bands'])
def test_array_cannot_be_validated_or_saved_for_other_phases(tmp_path, phase):
    with pytest.raises(ValueError, match='仅支持 convergence'):
        validate_resources(ARRAY, phase, PEAK_LIMITS, tests=6)
    with pytest.raises(ValueError, match='仅支持 convergence'):
        save_defaults(tmp_path, 'test', phase, ARRAY)
    assert not agent_paths.store_dir(tmp_path, 'submission_defaults').exists()


def test_sequential_menu_cannot_inherit_array_mode_from_scan(tmp_path):
    save_defaults(tmp_path, 'test', 'convergence', ARRAY)
    selected, output, prompts = menu(tmp_path, ['array', '1'], phase='scf', current=ARRAY, job='si')
    assert selected == {**ARRAY, 'parallel_tests': 1, 'submission_mode': 'allocation'}
    assert len(prompts) == 2 and any('模式未切换' in text for text in output)
    assert load_defaults(tmp_path, 'test', 'convergence') == ARRAY
    assert load_defaults(tmp_path, 'test', 'scf') is None


@pytest.mark.parametrize('changes', [{'nodes': 5}, {'cores': 95}])
def test_array_limits_must_cover_all_concurrently_running_elements(changes):
    with pytest.raises(ValueError, match='上限'):
        validate_resources(ARRAY, 'convergence', {**PEAK_LIMITS, **changes}, tests=6)
    validate_resources(ARRAY, 'convergence', PEAK_LIMITS, tests=6)


def test_array_multi_node_elements_are_checked_against_peak_node_grant():
    multi_node = {**ARRAY, 'nodes': 2}
    with pytest.raises(ValueError, match='节点峰值'):
        validate_resources(multi_node, 'convergence', PEAK_LIMITS, tests=6)
    validate_resources(multi_node, 'convergence', {**PEAK_LIMITS, 'nodes': 12}, tests=6)


def test_array_validation_passes_per_element_schema_to_worker(monkeypatch):
    captured = []
    monkeypatch.setattr('vasp_agent.remote.remote_worker.check_resources',
                        lambda script, limits: captured.append((script, limits)))
    selected = {**ARRAY, 'parallel_tests': 3}
    validate_resources(selected, 'convergence', PEAK_LIMITS, tests=4)
    assert '#SBATCH --nodes=1\n' in captured[0][0]
    assert '#SBATCH --ntasks=16\n' in captured[0][0]
    assert '#SBATCH --array=0-3%3' in captured[0][0]
    assert captured[0][1] == PEAK_LIMITS


@pytest.mark.parametrize('value', ['', 'ARRAY', 'batch', 'true'])
def test_invalid_modes_do_not_apply_or_save(tmp_path, value):
    selected, output, _ = menu(tmp_path, [f'submission_mode={value}', '1'], job='si')
    assert selected == RES and any('参数未应用' in text for text in output)
    assert not agent_paths.store_dir(tmp_path, 'submission_defaults').exists()


def test_mode_aliases_cannot_be_repeated():
    with pytest.raises(ValueError, match='不重复'):
        parse_custom('模式=array submission_mode=allocation', RES)


def test_one_confirmation_grants_array_peak_and_resumes_without_reasking(tmp_path):
    save_defaults(tmp_path, 'test', 'convergence', ARRAY)
    prompts, calls = [], []
    state = {'signature': 'a', 'resources': RES, 'execution_plan': {'tests': 6}}
    box = NS(workspace=tmp_path, plan_output=lambda _: None, resource_confirmations={},
             submission_limits=None, submission_target=None, allow_submit=False,
             log=lambda _: None)
    def read(prompt):
        assert not box.allow_submit and not any(action == 'configure_resources' for action, _ in calls)
        prompts.append(prompt)
        return '1'
    box.plan_reader = read
    def rpc(action, **kwargs):
        calls.append((action, kwargs))
        if action == 'configure_resources':
            assert kwargs['resources'] == ARRAY
            assert kwargs['limits'] == {**PEAK_LIMITS, 'server': 'test'}
            assert box.submission_target == ('test', 'si', 'convergence')
            state.update(signature='b', resources=kwargs['resources'])
            return {'signature': 'b', 'changed': True, 'resources': kwargs['resources']}
        assert action == 'resource_context'
        return state.copy()
    assert confirm(box, 'test', 'si', 'convergence', rpc, authorize=True)['status'] == 'awaiting_resource_review'
    assert confirm(box, 'test', 'si', 'convergence', rpc, authorize=True) is None
    assert len(prompts) == 1 and sum(action == 'configure_resources' for action, _ in calls) == 1
    assert box.submission_limits == {**PEAK_LIMITS, 'server': 'test'}
    assert box.allow_submit and box.max_submissions == 1


def test_array_apply_only_uses_preview_without_grant_or_submit(tmp_path):
    save_defaults(tmp_path, 'test', 'convergence', ARRAY)
    calls = []
    box = NS(workspace=tmp_path, plan_reader=lambda _: '1', plan_output=lambda _: None,
             resource_confirmations={}, submission_limits=None, allow_submit=False,
             submission_target=None, log=lambda _: None)
    def rpc(action, **kwargs):
        calls.append((action, kwargs))
        if action == 'configure_resources':
            assert kwargs['resources'] == ARRAY and not box.allow_submit
            return {'signature': 'b', 'changed': True}
        assert action == 'resource_context'
        return {'signature': 'a', 'resources': RES, 'execution_plan': {'tests': 6}}
    assert confirm(box, 'test', 'si', 'convergence', rpc, apply_only=True)['status'] == 'awaiting_resource_review'
    assert [action for action, _ in calls] == ['resource_context', 'configure_resources']
    assert box.submission_limits is None and box.submission_target is None and not box.allow_submit


def test_legacy_target_accepts_explicit_allocation_remote_response_without_reasking(tmp_path):
    calls = []
    state = {'signature': 'a', 'resources': {**RES, 'submission_mode': 'allocation'},
             'execution_plan': {'tests': 6}}
    prompts = []
    box = NS(workspace=tmp_path, plan_reader=lambda prompt: prompts.append(prompt) or '1',
             plan_output=lambda _: None, resource_confirmations={}, submission_limits=None,
             allow_submit=False, submission_target=None, log=lambda _: None)
    def rpc(action, **kwargs):
        calls.append(action)
        if action == 'configure_resources':
            assert kwargs['resources'] == RES
            state['signature'] = 'b'
            return {'signature': 'b', 'changed': True, 'resources': state['resources']}
        assert action == 'resource_context'
        return state.copy()
    result = confirm(box, 'test', 'si', 'convergence', rpc, apply_only=True, expected_resources=RES)
    assert result['status'] == 'awaiting_resource_review'
    assert confirm(box, 'test', 'si', 'convergence', rpc, apply_only=True, expected_resources=RES) is None
    assert len(prompts) == 1 and calls.count('configure_resources') == 1
    assert not box.allow_submit


def test_submit_defaults_normalizes_server_array_mode_for_sequential_tasks(tmp_path):
    c = commands(tmp_path, ['1', '2', '1'])
    path = tmp_path / 'servers/test.compute.json'
    config = json.loads(path.read_text())
    path.write_text(json.dumps({**config, 'submission_mode': 'array'}), encoding='utf-8')
    assert c.handle('/submit defaults') == 'handled'
    assert load_defaults(tmp_path, 'test', 'scf')['submission_mode'] == 'allocation'
    assert not c.agent.tools.allow_submit and c.agent.tools.submission_limits is None


def test_array_larger_than_limit_is_refused_not_truncated():
    from vasp_agent.permissions.submission_dialog import validate_resources
    from vasp_agent.remote.remote_worker import MAX_ARRAY_TESTS, array_layout
    resources = {'submission_mode': 'array', 'parallel_tests': 1, 'cores_per_test': 8, 'nodes': 1,
                 'partition': 'compute', 'walltime': '04:00:00'}
    limits = {'partition': 'compute', 'nodes': 1, 'cores': 64, 'walltime': '04:00:00'}
    with pytest.raises(ValueError, match='超过作业数组上限'):
        validate_resources(resources, 'convergence', limits, MAX_ARRAY_TESTS + 1)
    assert array_layout('#SBATCH --array=0-6%6\n')['task_count'] == 7
    for bad in ('0-7%2', '0-6%7', '0-2%4', '0-3%0', '0-5%03', '00-5%2'):
        with pytest.raises(ValueError):
            array_layout(f'#SBATCH --array={bad}\n')
