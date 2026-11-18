"""Offline conversation scenarios: real generated files, fake transport/model/potentials."""
import json
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

import vasp_agent.chat.ai_agent as ai
import vasp_agent.workflow.input_review as input_review
import vasp_agent.planning.parameter_plans as parameter_plans
import vasp_agent.remote.remote_worker as remote_worker
import vasp_agent.workflow.stage_gate as stage_gate
import vasp_agent.workflow.vasp_band_agent as core
from vasp_agent.remote.remote_compute import RemoteCompute
from test_ai_agent import FakeResponses, function, reply, prepare_args
from vasp_agent.common import agent_paths


@pytest.fixture
def rig(tmp_path, monkeypatch):
    (tmp_path / 'Si.POSCAR').write_bytes((Path(__file__).parents[1] / 'examples/Si.POSCAR').read_bytes())
    library = tmp_path / 'potentials/PBE'
    (library / 'Si').mkdir(parents=True)
    # Deliberately no trailing newline: source and assembled hashes must differ.
    (library / 'Si/POTCAR').write_text('synthetic potential, not licensed POTCAR content')
    monkeypatch.setattr('vasp_agent.workflow.potential_library.Potcar.from_str', lambda text: [NS(
        element='Si', functional='PBE', TITEL='synthetic Si title', enmax=245.345)])
    servers = tmp_path / 'servers'
    servers.mkdir()
    core.save(servers / 'test.json', {'host': 'example.invalid', 'user': 'tester', 'port': 22})
    core.save(servers / 'test.compute.json', dict(root='/cluster/root', python='/bin/python',
        potcar_library='/potentials', vasp_env='/env.sh'))
    root = tmp_path / 'remote'
    root.mkdir()
    from vasp_agent.remote.bundle import local_version
    core.save(root / 'deployment.json', {'release': str(Path(core.__file__).parent), 'version': local_version()})
    env = root / 'env.sh'
    env.write_text('#!/bin/bash\n')
    config = dict(root=str(root), python='/bin/python', potcar_library=str(library),
        vasp_env=str(env), partition='compute', ntasks=8, walltime='04:00:00')
    calls = []
    monkeypatch.setattr(remote_worker, 'inspect_environment', lambda *a: {'executable_sha256': 'fixture', 'environment_sha256': 'fixture', 'executable': '/fake/vasp_std'})

    def rpc(self, action, job_name=None, **params):
        if action == 'deployment_info':  # the read-only freshness check before prepare/preview
            return remote_worker.dispatch(dict(config=config, action=action))
        calls.append(action)
        assert action in ('prepare', 'preview', 'review_context', 'stage_context'), action
        return remote_worker.dispatch(dict(config=config, action=action, job_name=job_name, **params))

    def process(args, **kwargs):
        assert args[:2] == ['bash', '-n'], 'No SSH, sbatch, VASP, or other external execution allowed'
        return NS(returncode=0)

    monkeypatch.setattr(RemoteCompute, 'call', rpc)
    monkeypatch.setattr('subprocess.run', process)
    monkeypatch.setattr(stage_gate, 'confirm_physics', lambda *a, **kw: pytest.fail('No automatic physics confirmation'))
    box = ai.Toolbox(tmp_path)
    permission = box.permission_status()
    yield NS(box=box, root=root, calls=calls, config=config)
    assert box.permission_status() == permission
    assert box.submissions == 0
    assert not list(tmp_path.rglob('physics-model.json'))
    assert not list(tmp_path.rglob('*submission.json'))


def remote_args():
    return dict(server='test', job_name='si', structure='Si.POSCAR', encut=520,
                mesh=[8, 8, 8], nbands=16, relax_mode='cell', parallel_tests=None, cores_per_test=None)


def read_args(path='/cluster/root/jobs/si/potential-selection.json'):
    return dict(server='test', action='read', path=path)


def conversation(rig, replies, max_steps=10):
    ui_events = []
    responses = FakeResponses(replies)
    agent = ai.Agent(NS(responses=responses), 'offline-test', rig.box, max_steps=max_steps,
                     ui=NS(tool=lambda name, ok: ui_events.append((name, ok))))
    answer = agent.ask('准备并核对输入摘要，不提交。')
    outputs = [json.loads(x['output']) for x in agent.history if x.get('type') == 'function_call_output']
    return outputs, ui_events, agent, answer


@pytest.mark.parametrize('selected', [False, True])
def test_prepare_conversation_returns_generated_remote_summary(rig, selected):
    if selected:
        request = remote_args()
        draft = parameter_plans.propose(rig.box, request, None, None, 'tool_default', 'offline test')
        call = function('prepare_scan_plan', {'plan_id': draft['id']})
    else:
        call = function('remote_prepare', remote_args())
    outputs, _, _, _ = conversation(rig, [reply(call), reply(text='已准备，尚未收敛验证。')])
    summary = outputs[0]['result']['preparation_summary']
    folder = rig.root / 'jobs/si'
    assert summary['potential_metadata'] == json.loads((folder / 'potential-selection.json').read_text())
    assert summary['scans']['encuts'] == [520, 598, 676]
    assert summary['resources']['ntasks'] == 8
    assert summary['ready'] and summary['hashes_verified'] and not summary['missing_fields']
    assert summary['potential_metadata'][0]['sha256'] != core.digest(folder / 'POTCAR')
    assert 'synthetic potential, not licensed' not in json.dumps(outputs)
    assert 'review_context' not in rig.calls


def test_denial_recovers_read_only_and_stage_gate_stays_closed(rig):
    rig.box.remote_prepare(**remote_args())
    outputs, ui, agent, _ = conversation(rig, [reply(function('remote_operation', read_args())),
        reply(function('get_stage_review', dict(server='test', job_name='si'), 'c2')), reply(text='摘要已读取，放行仍需物理确认。')])
    assert not outputs[0]['ok']
    recovered = outputs[0]['recovery']['output']
    assert recovered['ok'] and recovered['result']['potential_metadata'][0]['element'] == 'Si'
    assert outputs[1]['error_code'] == 'PHYSICS_CONFIRMATION_REQUIRED'
    assert ui[:2] == [('remote_operation', False), ('get_input_review (只读恢复)', True)]
    audit = [json.loads(line) for line in rig.box.audit.read_text(encoding='utf-8').splitlines()]
    assert any(x.get('tool') == 'remote_operation' and not x['output']['ok'] for x in audit)
    assert any(x.get('tool') == 'get_input_review' and x['output']['ok'] for x in audit)
    assert json.loads(agent.file.read_text(encoding='utf-8')) == agent.history


@pytest.mark.parametrize('path', [
    '/unknown/jobs/si/potential-selection.json',
    '/cluster/root/jobs/si/nested/potential-selection.json',
    '/cluster/root/jobs/si/../si/potential-selection.json',
    '/cluster/root/jobs/si//potential-selection.json',
    '/cluster/root/jobs/si/./potential-selection.json',
    '/cluster/root/jobs/si/POTCAR', '/cluster/root/jobs/si/private.key',
])
def test_unknown_or_unsafe_path_has_no_recovery(rig, path):
    outputs, _, _, _ = conversation(rig, [reply(function('remote_operation', read_args(path))), reply(text='不能安全映射任务。')])
    assert not outputs[0]['ok'] and not outputs[0]['recoverable']
    assert 'recovery' not in outputs[0] and not rig.calls


@pytest.mark.parametrize('damage,code', [('missing', 'FILE_NOT_FOUND'), ('changed', 'INPUT_HASH_MISMATCH'),
    ('unbound', 'UNVERIFIED_METADATA'), ('empty', 'METADATA_INVALID')])
def test_failed_recovery_is_bounded_and_preserves_actual_reason(rig, damage, code):
    rig.box.remote_prepare(**remote_args())
    folder = rig.root / 'jobs/si'
    metadata = folder / 'potential-selection.json'
    manifest = json.loads((folder / 'manifest.json').read_text())
    if damage == 'missing':
        metadata.unlink()
    elif damage == 'changed':
        metadata.write_text('[]')
    elif damage == 'unbound':
        del manifest['hashes']['potential-selection.json']
        core.save(folder / 'manifest.json', manifest)
    else:
        metadata.write_text('[]')
        manifest['hashes']['potential-selection.json'] = core.digest(metadata)
        core.save(folder / 'manifest.json', manifest)
    outputs, ui, _, answer = conversation(rig,
        [reply(function('remote_operation', read_args(), f'c{i}')) for i in range(3)], max_steps=3)
    assert outputs[0]['recovery']['output']['error_code'] == code
    assert not outputs[0]['recovery']['output']['ok']
    assert all(o['recovery']['skipped'] for o in outputs[1:])
    assert rig.calls.count('review_context') == 1 and rig.calls.count('prepare') == 1
    assert not any(ok for _, ok in ui)
    assert '上限' in answer


def test_global_recovery_budget(rig):
    outputs, _, _, _ = conversation(rig, [reply(*[
        function('remote_operation', read_args(f'/cluster/root/jobs/si{i}/potential-selection.json'), f'c{i}')
        for i in range(4)]), reply(text='目录不存在。')])
    assert rig.calls == ['review_context', 'review_context']
    assert outputs[0]['recovery']['output']['error_code'] == 'FILE_NOT_FOUND'
    assert all(o['recovery']['skipped'] for o in outputs[2:])


@pytest.mark.parametrize('selected', [False, True])
def test_local_prepare_summary_and_preview(rig, selected):
    # Test both no-POTCAR preview and ready local preparation.
    rig.box.potcar_library = None
    if selected:
        draft = parameter_plans.propose(rig.box, {**remote_args(), 'server': None}, None, None, 'tool_default', '')
        result = parameter_plans.prepare(rig.box, draft['id'])
    else:
        result = rig.box.prepare_calculation(**prepare_args('si'))
    summary = result['preparation_summary']
    assert summary['preview_only'] and not summary['ready']
    assert summary['missing_fields'] == ['potential_metadata']
    rig.box.potcar_library = rig.box.workspace / 'potentials/PBE'
    args = prepare_args('ready')
    outputs, _, _, _ = conversation(rig, [reply(function('prepare_calculation', args)), reply(text='准备完成。')])
    assert outputs[0]['result']['preparation_summary']['ready']
    data = rig.box.get_input_review(None, 'ready')
    with pytest.raises(ValueError, match='record_stage_review'):
        input_review.record(rig.box.job('ready'), data['snapshot'], 'pass', 'cannot bypass', 'test')
    assert rig.box.call('get_stage_review', dict(server=None, job_name='ready'))['error_code'] == 'PHYSICS_CONFIRMATION_REQUIRED'
    assert rig.box.call('submit_stage', dict(server=None, job_name='ready', phase='convergence'))['error_code'] == 'SUBMISSION_NOT_AUTHORIZED'


def test_old_release_summary_is_explicitly_unavailable(rig, monkeypatch):
    monkeypatch.delattr(input_review, 'preparation_summary')
    result = rig.box.remote_prepare(**remote_args())
    assert result['prepared']
    assert result['preparation_summary']['available'] is False
    assert 'hashes_verified' not in result['preparation_summary']
    assert result['resources']['ntasks'] == 8


def test_retry_keeps_completed_recovery_and_does_not_replay_prepare(rig):
    steps = iter([reply(function('remote_prepare', remote_args())),
                  reply(function('remote_operation', read_args(), 'c2')),
                  TimeoutError('offline timeout'), reply(text='继续使用已保存摘要。')])
    def create(*args):
        result = next(steps)
        if isinstance(result, Exception):
            raise result
        return result
    agent = ai.Agent(None, 'offline', rig.box, adapter=NS(create=create), ui=NS(tool=lambda *a: None))
    assert '/retry' in agent.ask('准备后读取，先不提交')
    assert agent.ask() == '继续使用已保存摘要。'
    assert rig.calls == ['prepare', 'review_context']


@pytest.mark.parametrize('returncode,response,code', [
    (1, {'ok': False, 'error': 'localized independent message', 'error_code': 'PHYSICS_CONFIRMATION_REQUIRED'}, 'PHYSICS_CONFIRMATION_REQUIRED'),
    (1, {'ok': False, 'error': 'legacy worker message'}, 'REMOTE_ERROR'),
    (255, None, 'SSH_FAILED'), (0, None, 'REMOTE_RESPONSE_INVALID'),
])
def test_wire_error_identity_and_legacy_fallback(tmp_path, monkeypatch, returncode, response, code):
    remote = object.__new__(RemoteCompute)
    remote.config = {'python': '/bin/python'}
    remote.server = 'test'
    remote.servers = NS(ssh_args=lambda server: ['ssh', 'example.invalid'])
    monkeypatch.setattr('subprocess.run', lambda *a, **kw: NS(returncode=returncode,
        stdout=json.dumps(response) if response else 'invalid response', stderr='offline failure'))
    with pytest.raises(core.ToolError) as error:
        remote.call('stage_context', 'si')
    assert error.value.error_code == code
    if response:
        assert str(error.value) == response['error']


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('residual_blank', [False, True])
def test_preview_then_suggestion_conversation_selects_once_and_prepares(rig, legacy, residual_blank):
    """Reproduce the user's two calls, including compatibility with the old API."""
    prompts, menu = [], []
    def reader(prompt):
        prompts.append(prompt)
        if residual_blank and len(prompts) == 1:
            return '\r\n'
        assert len(prompts) == (2 if residual_blank else 1), 'The operator must select only once'
        return '2'
    rig.box.plan_reader = reader
    rig.box.plan_output = menu.append
    args = {**remote_args(), 'job_name': 'si-multinode-02'}
    suggest = {**args, 'encuts': None, 'meshes': [[12,12,12],[14,14,14],[16,16,16]],
               'origin': 'ai_suggestion', 'rationale': 'Compare denser grids; more cost, convergence remains untested.'}
    before = {str(p.relative_to(rig.root)): p.read_bytes() for p in rig.root.rglob('*') if p.is_file()}
    call_index = 0
    def create(model, instructions, history, schemas):
        nonlocal call_index
        call_index += 1
        if call_index == 1:
            assert 'preview_scan_defaults' in {s['name'] for s in schemas}
            return reply(function('propose_scan_plan', {**args, 'encuts': None, 'meshes': None,
                'origin': 'tool_default', 'rationale': ''}) if legacy else function('preview_scan_defaults', args))
        output = json.loads(history[-1]['output'])
        assert output['ok'], output
        if call_index == 2:
            assert output['result']['defaults']['meshes'] == [[8,8,8],[10,10,10],[12,12,12]]
            assert not prompts
            assert not (rig.root / 'jobs').exists() and not (rig.root / 'uploads').exists()
            assert {str(p.relative_to(rig.root)): p.read_bytes() for p in rig.root.rglob('*') if p.is_file()} == before
            if not legacy:
                assert output['result']['plan_saved'] is False
                assert not list(agent_paths.store_dir(rig.box.workspace, 'scan_plans').glob('*.json'))
            return reply(function('propose_scan_plan', suggest, 'c2'))
        if call_index == 3:
            result = output['result']
            assert result['suggestion']['meshes'] == suggest['meshes']
            assert result['selected']['meshes'] == suggest['meshes']
            assert result['choice'] == 'operator_accept_ai'
            assert result['selection']['operator_confirmed']
            # Repeated identical request must not ask again.
            return reply(function('propose_scan_plan', suggest, 'c3'))
        if call_index == 4:
            return reply(function('prepare_scan_plan', {'plan_id': output['result']['id']}, 'c4'))
        assert output['result']['preparation_summary']['scans']['meshes'] == suggest['meshes']
        return reply(text='已按菜单选择准备并核对摘要，未提交。')
    agent = ai.Agent(None, 'offline', rig.box, adapter=NS(create=create), ui=NS(tool=lambda *a: None))
    assert '未提交' in agent.ask('先读取工具实际默认扫描参数，再提出建议；若有差异，通过参数选择菜单让我选择。先不提交。')
    assert len(prompts) == (2 if residual_blank else 1) and rig.calls == ['preview', 'preview', 'prepare']
    assert any('8³ / 10³' in line for line in menu)
    assert any('14³ / 16³' in line for line in menu)
    from pymatgen.io.vasp.inputs import Kpoints
    for i, grid in enumerate(suggest['meshes']):
        file = rig.root / f'jobs/si-multinode-02/convergence/mesh-{i}/KPOINTS'
        assert list(Kpoints.from_file(file).kpts[0]) == grid
