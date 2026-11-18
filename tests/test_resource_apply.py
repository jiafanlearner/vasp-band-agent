import json
from types import SimpleNamespace as NS
import pytest
from vasp_agent.chat.ai_agent import Toolbox
from vasp_agent.permissions.submission_dialog import confirm
from vasp_agent.remote import remote_worker as worker
from test_submission_recovery import target, recover

RES = dict(parallel_tests=6, cores_per_test=16, nodes=2, partition='compute', walltime='04:00:00')


def test_apply_without_submit_permission_or_review_then_no_reasking(tmp_path, monkeypatch):
    from vasp_agent.remote.remote_compute import RemoteCompute
    from vasp_agent.permissions import resource_menu
    monkeypatch.setattr(RemoteCompute, '__init__', lambda *a: None)
    state = {'signature': 'before', 'resources': {**RES, 'nodes': 1}, 'execution_plan': {'tests': 6}}
    calls = []
    def rpc(self, action, job, **kw):
        calls.append(action)
        if action == 'configure_resources':
            assert kw['resources'] == RES
            state.update(signature='after', resources=RES)
            return {**state, 'changed': True}
        assert action == 'resource_context'
        return state.copy()
    monkeypatch.setattr(RemoteCompute, 'call', rpc)
    selections = []
    monkeypatch.setattr(resource_menu, 'resource_menu', lambda *a, **k: selections.append(1) or RES.copy())
    box = Toolbox(tmp_path)
    box.plan_reader = lambda _: '1'
    box.plan_output = lambda _: None
    result = box.call('apply_submission_resources', {'server': 'test', 'job_name': 'si', 'phase': 'convergence', 'resources': RES})
    assert result['ok'] and result['result']['status'] == 'awaiting_resource_review'
    assert not box.allow_submit and box.submissions == 0
    box.apply_submission_resources('test', 'si', 'convergence', RES)
    assert selections == [1] and calls.count('configure_resources') == 1
    assert 'submission_preflight' not in calls and 'submit' not in calls


def test_existing_exact_operator_selection_reused_without_menu(tmp_path, monkeypatch):
    box = Toolbox(tmp_path)
    box.plan_output = lambda _: None
    box.confirmed_resource_selections[('test', 'si', 'convergence')] = {'signature': 'old', 'resources': RES}
    calls = []
    def rpc(action, **kwargs):
        calls.append(action)
        if action == 'resource_context':
            return {'signature': 'old', 'resources': {**RES, 'nodes': 1}}
        assert kwargs['resources'] == RES
        return {'signature': 'new', 'changed': True}
    assert confirm(box, 'test', 'si', 'convergence', rpc, apply_only=True)['operator_confirmed']
    assert calls == ['resource_context', 'configure_resources']
    assert not box.allow_submit


@pytest.mark.parametrize('different', ['signature', 'task'])
def test_stale_or_other_task_selection_cannot_authorize(tmp_path, different):
    box = Toolbox(tmp_path)
    box.confirmed_resource_selections[('test', 'other' if different == 'task' else 'si', 'convergence')] = {'signature': 'old', 'resources': RES}
    def rpc(action, **kw):
        assert action == 'resource_context'
        return {'signature': 'new' if different == 'signature' else 'old', 'resources': RES}
    with pytest.raises(ValueError, match='确认'):
        confirm(box, 'test', 'si', 'convergence', rpc, apply_only=True)


def test_recovered_task_configures_before_review_but_submit_still_blocked(target, monkeypatch):
    from vasp_agent.workflow import vasp_band_agent as core, stage_gate, full_workflow
    folder, _ = target
    (folder / 'config.json').write_text(json.dumps({**RES, 'nodes': 1, 'nbands': 16}))
    (folder / 'manifest.json').write_text('{"stage_gates":true,"hashes":{}}')
    (folder / 'submit.slurm').write_text('#!/bin/bash\n#SBATCH --partition=compute\n#SBATCH --nodes=1\n#SBATCH --ntasks=96\n#SBATCH --time=04:00:00\nexport OMP_NUM_THREADS=1\nsrun vasp_std\n')
    recover(folder)
    monkeypatch.setattr(core, 'verify', lambda _: None)
    monkeypatch.setattr(stage_gate, 'current', lambda _: 'convergence')
    monkeypatch.setattr(full_workflow, 'RESOURCE_LAYOUT_VERSION', 2)
    before = worker.resource_context(folder, 'convergence')
    limits = dict(nodes=2, cores=96, partition='compute', walltime='04:00:00')
    after = worker.configure_resources(folder, 'convergence', RES, before['signature'], limits)
    assert after['changed'] and after['resources']['nodes'] == 2
    assert '--ntasks-per-node=48' in (folder / 'submit.slurm').read_text()
    assert json.loads((folder / 'config.json').read_text())['nbands'] == 16
    with pytest.raises(ValueError, match='重新审阅'):
        worker.recovery_barrier(folder, 'convergence', require_new_review=True)
    assert not (folder / 'submissions/convergence.json').exists()


def test_apply_refuses_to_replay_uncertain_resource_write(tmp_path):
    box = Toolbox(tmp_path)
    box.resource_confirmations[('test', 'si', 'convergence')] = {'operation_id': 'a'*32, 'resources': RES}
    def rpc(action, **kw):
        assert action == 'resource_context'
        return {'signature': 'old', 'resources': RES}
    assert confirm(box, 'test', 'si', 'convergence', rpc, apply_only=True)['status'] == 'resource_update_uncertain'


def test_apply_menu_never_claims_submit_authorization():
    from vasp_agent.permissions.resource_menu import resource_menu
    output = []
    assert resource_menu(None, 'test', 'convergence', RES, lambda _: '1', output.append,
                         job='si', apply_only=True) == RES
    assert any('仅应用以上资源' in line for line in output)
    assert not any('确认并授权本阶段提交' in line for line in output)


@pytest.mark.parametrize('case', ['old_signature', 'old_selection', 'old_default', 'wrong_return', 'wrong_readback'])
def test_two_node_target_cannot_succeed_with_one_node(tmp_path, monkeypatch, case):
    from vasp_agent.permissions.resource_menu import save_defaults
    box = Toolbox(tmp_path)
    box.plan_output = lambda _: None
    asked = []
    box.plan_reader = lambda _: asked.append(1) or '1'
    old = {**RES, 'nodes': 1}
    if case == 'old_signature': box.resource_confirmations[('test', 'si', 'convergence')] = 'before'
    if case == 'old_selection':
        box.confirmed_resource_selections[('test', 'si', 'convergence')] = {'signature': 'before', 'resources': old}
    if case == 'old_default': save_defaults(tmp_path, 'test', 'convergence', old)
    state = {'signature': 'before', 'resources': old}
    writes = []
    def rpc(action, **kwargs):
        if action == 'resource_context': return state.copy()
        assert action == 'configure_resources' and kwargs['resources'] == RES
        writes.append(kwargs)
        state.update(signature='after', resources=old if case == 'wrong_readback' else RES)
        return {'signature': 'after', 'changed': True, 'resources': old if case == 'wrong_return' else RES}
    result = confirm(box, 'test', 'si', 'convergence', rpc, apply_only=True, expected_resources=RES)
    assert len(writes) == 1 and asked == [1]
    if case.startswith('wrong'):
        assert result['status'] == 'resource_application_mismatch'
        assert isinstance(box.resource_confirmations[('test', 'si', 'convergence')], dict)
    else:
        assert result['status'] == 'awaiting_resource_review' and result['resources']['nodes'] == 2
    assert not box.allow_submit


def test_pending_old_operation_does_not_claim_new_target(tmp_path):
    box = Toolbox(tmp_path)
    old = {**RES, 'nodes': 1}
    box.resource_confirmations[('test', 'si', 'convergence')] = {'operation_id': 'a'*32, 'resources': old}
    def rpc(action, **kwargs):
        assert action == 'resource_context'
        return {'signature': 'old', 'resources': old,
                'resource_change': {'operation_id': 'a'*32, 'phase': 'convergence', 'signature': 'old'}}
    assert confirm(box, 'test', 'si', 'convergence', rpc, apply_only=True,
                   expected_resources=RES)['status'] == 'resource_target_mismatch'


def test_operator_different_selection_does_not_write_target(tmp_path, monkeypatch):
    from vasp_agent.permissions import resource_menu
    box = Toolbox(tmp_path)
    box.plan_reader = lambda _: '1'
    box.plan_output = lambda _: None
    monkeypatch.setattr(resource_menu, 'resource_menu', lambda *a, **kw: {**RES, 'nodes': 1})
    def rpc(action, **kw):
        assert action == 'resource_context'
        return {'signature': 'old', 'resources': {**RES, 'nodes': 1}}
    result = confirm(box, 'test', 'si', 'convergence', rpc, apply_only=True, expected_resources=RES)
    assert result['status'] == 'resource_selection_differs' and not result['applied']


def test_request_submission_with_target_is_one_menu_then_no_second_prompt(tmp_path, monkeypatch):
    from vasp_agent.remote.remote_compute import RemoteCompute
    from vasp_agent.permissions import resource_menu
    monkeypatch.setattr(RemoteCompute, '__init__', lambda *a: None)
    target = dict(parallel_tests=1, cores_per_test=32, nodes=1, partition='compute', walltime='04:00:00')
    state = {'signature': 'before', 'resources': {**target, 'cores_per_test': 8}}
    calls = []
    def rpc(self, action, job, **kw):
        calls.append(action)
        if action == 'configure_and_submit':
            # A write that changes reviewed inputs comes back without submitting.
            state.update(signature='after', resources=kw['resources'])
            return {**state, 'changed': True, 'submission': None, 'not_submitted': 'needs_review'}
        assert action == 'resource_context'
        return state.copy()
    monkeypatch.setattr(RemoteCompute, 'call', rpc)
    menus = []
    monkeypatch.setattr(resource_menu, 'resource_menu', lambda ws, server, phase, defaults, *a, **k:
                        menus.append(defaults) or defaults.copy())
    box = Toolbox(tmp_path)
    prompts, shown = [], []
    box.plan_reader = lambda p: prompts.append(p) or '1'
    box.plan_output = shown.append
    result = box.call('request_submission', {'server': 'test', 'job_name': 'la', 'phase': 'relaxation', 'resources': target})
    assert result['ok'] and result['result']['status'] == 'awaiting_resource_review'
    assert menus == [target] and box.allow_submit and box.submission_target == ('test', 'la', 'relaxation')
    assert calls.count('configure_and_submit') == 1 and box.submissions == 0
    # After review the same target needs no further question.
    assert confirm(box, 'test', 'la', 'relaxation', lambda a, **kw: rpc(None, a, 'la', **kw), authorize=True) is None
    assert prompts == [] and len(menus) == 1


def test_reused_selection_asks_for_grant_in_one_compact_line(tmp_path):
    box = Toolbox(tmp_path)
    shown, prompts = [], []
    box.plan_output = shown.append
    box.plan_reader = lambda p: prompts.append(p) or '1'
    key = ('test', 'la', 'relaxation')
    target = dict(parallel_tests=1, cores_per_test=32, nodes=1, partition='compute', walltime='04:00:00')
    box.confirmed_resource_selections[key] = {'signature': 'sig', 'resources': target}
    box.resource_confirmations[key] = 'sig'
    def rpc(action, **kw):
        if action == 'resource_context':
            return {'signature': 'sig', 'resources': target}
        return {'signature': 'sig', 'resources': kw['resources'], 'changed': False}
    assert confirm(box, 'test', 'la', 'relaxation', rpc, authorize=True) is None
    assert box.allow_submit and prompts == ['选择：']
    assert not any('程序建议' in line or '提交前资源确认' in line for line in shown)
    assert sum('沿用已确认资源' in line for line in shown) == 1
