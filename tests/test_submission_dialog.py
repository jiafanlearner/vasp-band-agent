import json
from types import SimpleNamespace as NS

import pytest
from vasp_agent.permissions.submission_dialog import confirm
from vasp_agent.remote.remote_worker import configure_resources, resource_context, sha

LIMITS = dict(server='test', partition='compute', nodes=1, cores=24, walltime='04:00:00')
RESOURCES = dict(parallel_tests=1, cores_per_test=8, nodes=1, partition='compute', walltime='04:00:00')


def box(answers):
    values = iter(['3', *answers])
    return NS(plan_reader=lambda _: next(values), plan_output=lambda _: None, resource_confirmations={},
              submission_limits=LIMITS, log=lambda _: None)


def test_confirm_update_review_resume_without_reasking():
    b = box(['', '', '', '', '', '1'])
    calls = []
    signature = 'before'
    def rpc(action, **kwargs):
        nonlocal signature
        calls.append(action)
        if action == 'configure_resources':
            assert kwargs['resources']['parallel_tests'] == 3
            signature = 'after'
            return {'signature': signature, 'changed': True}
        return {'signature': signature, 'resources': RESOURCES}
    result = confirm(b, 'test', 'si', 'convergence', rpc)
    assert result['status'] == 'awaiting_resource_review'
    assert confirm(b, 'test', 'si', 'convergence', rpc) is None
    assert calls.count('configure_resources') == 1


def test_cancel_does_not_apply():
    b = box(['q'])
    calls = []
    def rpc(action, **kwargs):
        calls.append(action)
        return {'signature': 'a', 'resources': RESOURCES}
    with pytest.raises(ValueError, match='取消'):
        confirm(b, 'test', 'si', 'convergence', rpc)
    assert calls == ['resource_context'] and not b.resource_confirmations


def test_noninteractive_cannot_submit():
    b = box([])
    b.plan_reader = None
    with pytest.raises(ValueError, match='交互'):
        confirm(b, 'test', 'si', 'scf', lambda *a, **kw: {'signature': 'a', 'resources': RESOURCES})


def test_sequential_phase_and_edit_again():
    b = box(['8', '1', '', '', '3', '8', '1', '', '', '1'])
    def rpc(action, **kwargs):
        if action == 'configure_resources':
            assert kwargs['resources']['parallel_tests'] == 1
            return {'signature': 'a', 'changed': False}
        return {'signature': 'a', 'resources': RESOURCES}
    assert confirm(b, 'test', 'si', 'scf', rpc) is None


def test_applies_config_and_script_preserves_physics(tmp_path, monkeypatch):
    import vasp_agent.workflow.vasp_band_agent as core
    monkeypatch.setattr(core, 'verify', lambda _: None)
    config = dict(RESOURCES, encuts=[520, 598, 676], nbands=16)
    (tmp_path / 'config.json').write_text(json.dumps(config))
    (tmp_path / 'manifest.json').write_text('{"hashes":{}}')
    (tmp_path / 'submit.slurm').write_text('#!/bin/bash\n#SBATCH --partition=compute\n#SBATCH --nodes=1\n#SBATCH --ntasks=8\n#SBATCH --time=04:00:00\nexport OMP_NUM_THREADS=1\nsrun vasp_std\n')
    before = resource_context(tmp_path)
    selected = {**RESOURCES, 'parallel_tests': 3}
    result = configure_resources(tmp_path, None, selected, before['signature'], LIMITS)
    assert result['changed']
    saved = json.loads((tmp_path / 'config.json').read_text())
    assert saved['parallel_tests'] == 3 and saved['encuts'] == config['encuts'] and saved['nbands'] == 16
    assert '--ntasks=24' in (tmp_path / 'submit.slurm').read_text()

    assert json.loads((tmp_path / 'manifest.json').read_text())['hashes']['config.json'] == sha(tmp_path / 'config.json')
    with pytest.raises(ValueError, match='变化'):
        configure_resources(tmp_path, None, selected, before['signature'], LIMITS)
    with pytest.raises(ValueError, match='上限'):
        configure_resources(tmp_path, None, {**selected, 'parallel_tests': 6}, result['signature'], LIMITS)
    assert '--ntasks=24' in (tmp_path / 'submit.slurm').read_text()


def test_ai_suggestion_used_and_bad_integer_reprompted():
    b = box(['wrong', '', '', '', '', '', '3', '', '', '', '', '', '1'])
    b.resource_suggestions = {('test', 'si', 'convergence'): {
        'signature': 'a', 'resources': {**RESOURCES, 'parallel_tests': 2, 'cores_per_test': 12},
        'rationale': 'two independent tests within 24 cores'}}
    output = []
    b.plan_output = output.append
    def rpc(action, **kwargs):
        if action == 'configure_resources':
            assert kwargs['resources']['parallel_tests'] == 2
            assert kwargs['resources']['cores_per_test'] == 12
            return {'signature': 'b', 'changed': True}
        return {'signature': 'a', 'resources': RESOURCES}
    assert confirm(b, 'test', 'si', 'convergence', rpc)['operator_confirmed']
    assert any('AI 建议' in s for s in output)
    assert any('请输入整数' in s for s in output)


def test_proposal_tool_checks_signature_and_does_not_confirm(tmp_path, monkeypatch):
    from vasp_agent.chat.ai_agent import Toolbox
    b = Toolbox(tmp_path)
    b.submission_limits = LIMITS
    monkeypatch.setattr(b, 'get_submission_resources', lambda *a: {'signature': 'a', 'resources': RESOURCES})
    result = b.recommend_submission_resources('test', 'si', 'convergence', 'a', 3, 8, 'independent scans')
    assert not result['operator_confirmed'] and not b.resource_confirmations
    with pytest.raises(ValueError, match='变化'):
        b.recommend_submission_resources('test', 'si', 'convergence', 'old', 3, 8, 'reason')
    with pytest.raises(ValueError, match='上限'):
        b.recommend_submission_resources('test', 'si', 'convergence', 'a', 6, 8, 'reason')


def test_cli_submit_uses_one_confirmation_menu(tmp_path, monkeypatch):
    import vasp_agent.remote.remote_compute as remote_compute
    from test_submission_permissions import commands
    commands(tmp_path, [])  # Saved fake server configuration.
    answers = iter(['1'])
    calls = []
    def rpc(self, action, job, **kwargs):
        calls.append(action)
        if action == 'resource_context':
            return {'signature': 'a', 'resources': RESOURCES}
        assert action == 'configure_and_submit'
        return {'signature': 'a', 'changed': False, 'resources': RESOURCES,
                'submission': {'job_id': 'test-only', 'status': 'submitted'}}
    monkeypatch.setattr(remote_compute.RemoteCompute, 'call', rpc)
    result = remote_compute.interactive_submit(tmp_path, 'test', 'si', 'convergence', reader=lambda _: next(answers), output=lambda _: None)
    assert result['job_id'] == 'test-only'
    # One resource read per request: confirm() returning None is the authorization (chat#6).
    # Write and submit share one remote call once the menu granted this target.
    assert calls == ['resource_context', 'configure_and_submit']


def test_cli_noninteractive_rejected(tmp_path, monkeypatch):
    import sys
    from vasp_agent.remote.remote_compute import interactive_submit
    monkeypatch.setattr(sys, 'stdin', NS(isatty=lambda: False))
    with pytest.raises(ValueError, match='交互终端'):
        interactive_submit(tmp_path, 'test', 'si', 'convergence')


def test_defaults_shrink_old_resources_to_current_limits():
    b = box(['', '', '', '', '', '1'])
    b.submission_limits = {**LIMITS, 'cores': 16, 'nodes': 1}
    old = {**RESOURCES, 'cores_per_test': 96, 'nodes': 4, 'walltime': '10:00:00', 'partition': 'other'}
    def rpc(action, **kwargs):
        if action == 'configure_resources':
            assert kwargs['resources'] == {**RESOURCES, 'cores_per_test': 16}
            return {'signature': 'b', 'changed': True}
        return {'signature': 'a', 'resources': old, 'execution_plan': {'tests': 3}}
    assert confirm(b, 'test', 'si', 'convergence', rpc)['operator_confirmed']


def test_defaults_never_exceed_remaining_tests():
    b = box(['', '', '', '', '', '1'])
    def rpc(action, **kwargs):
        if action == 'configure_resources':
            assert kwargs['resources']['parallel_tests'] == 1
            return {'signature': 'b', 'changed': True}
        return {'signature': 'a', 'resources': RESOURCES, 'execution_plan': {'tests': 1}}
    confirm(b, 'test', 'si', 'convergence', rpc)


def test_lost_resource_response_recovers_by_matching_receipt_only():
    b = box(['', '', '', '', '', '1'])
    state = {'signature': 'before', 'resources': RESOURCES}
    writes = []
    def rpc(action, **kw):
        if action == 'configure_resources':
            writes.append(action)
            state.update(signature='after', resources=kw['resources'], resource_change={
                'operation_id': kw['operation_id'], 'phase': 'convergence', 'signature': 'after'})
            raise TimeoutError('response lost after commit')
        return state
    with pytest.raises(TimeoutError):
        confirm(b, 'test', 'si', 'convergence', rpc)
    result = confirm(b, 'test', 'si', 'convergence', rpc)
    assert result['recovered_after_disconnect']
    assert confirm(b, 'test', 'si', 'convergence', rpc) is None
    assert len(writes) == 1


def test_missing_resource_receipt_never_replays_or_prompts():
    b = box([])
    b.resource_confirmations[('test', 'si', 'convergence')] = {'operation_id': 'a'*32, 'resources': RESOURCES}
    assert confirm(b, 'test', 'si', 'convergence', lambda *a, **kw: {'signature': 'x', 'resources': RESOURCES})['status'] == 'resource_update_uncertain'


def dialog_run(answers, *, tests=6, limits=None):
    """All resource writes are simulated and recorded, never sent to SSH."""
    b = box([])
    b.submission_limits = limits or {**LIMITS, 'cores': 65536, 'nodes': 256}
    prompts, output, calls = [], [], []
    values = iter(['3', *answers])
    def read(prompt):
        prompts.append(prompt)
        assert not any(action == 'configure_resources' for action, _ in calls)
        return next(values)
    b.plan_reader = read
    b.plan_output = output.append
    def rpc(action, **kwargs):
        calls.append((action, kwargs))
        if action == 'configure_resources':
            return {'signature': 'b', 'changed': True}
        return {'signature': 'a', 'resources': RESOURCES, 'execution_plan': {'tests': tests}}
    return b, prompts, output, calls, rpc


def test_bad_parallel_and_nodes_reprompt_only_the_invalid_field():
    b, prompts, output, calls, rpc = dialog_run(
        ['9', '6', '16', '144', '96', '48', '6', '2', '', '', '1'])
    result = confirm(b, 'test', 'si', 'convergence', rpc)
    assert result['resources'] == {**RESOURCES, 'parallel_tests': 6, 'cores_per_test': 16, 'nodes': 2}
    assert sum(p.startswith('同时运行') for p in prompts) == 2
    assert sum(p.startswith('每组 MPI') for p in prompts) == 1
    assert sum(p.startswith('申请的物理节点数') for p in prompts) == 5
    assert sum(p.startswith('分区') for p in prompts) == 1
    assert [action for action, _ in calls] == ['resource_context', 'configure_resources']
    shown = '\n'.join(output)
    assert '6 组 × 16 进程 = 96 核' in shown
    assert '节点数必须整除 16' in shown
    assert '1, 2, 4, 8, 16' in shown
    assert '每节点 48 MPI，每组每节点 8' in shown


def test_remaining_test_count_rejects_excess_parallelism_immediately():
    b, prompts, output, calls, rpc = dialog_run(['6', '3', '8', '1', '', '', '1'], tests=3)
    assert confirm(b, 'test', 'si', 'convergence', rpc)['resources']['parallel_tests'] == 3
    assert prompts[1].startswith('同时运行') and prompts[2].startswith('同时运行')
    assert any('最多可并发 3 组' in line for line in output)


def test_total_core_partition_and_time_errors_keep_prior_fields():
    b, prompts, output, calls, rpc = dialog_run(
        ['3', '16', '8', '1', 'other', '', 'bad', '05:00:00', '', '1'], limits=LIMITS)
    result = confirm(b, 'test', 'si', 'convergence', rpc)
    assert result['resources'] == {**RESOURCES, 'parallel_tests': 3}
    assert sum(p.startswith('同时运行') for p in prompts) == 1
    assert sum(p.startswith('每组 MPI') for p in prompts) == 2
    assert sum(p.startswith('申请的物理节点数') for p in prompts) == 1
    assert sum(p.startswith('分区') for p in prompts) == 2
    assert sum(p.startswith('运行时限') for p in prompts) == 3
    shown = '\n'.join(output)
    assert '超过总核数上限 24' in shown and '已授权分区为 compute' in shown
    assert '超过授权上限 04:00:00' in shown
    assert b.submission_limits == LIMITS


def test_cancel_after_bad_nodes_never_writes_or_records_confirmation():
    b, prompts, output, calls, rpc = dialog_run(['6', '16', '96', 'q'])
    original_limits = b.submission_limits.copy()
    with pytest.raises(ValueError, match='取消'):
        confirm(b, 'test', 'si', 'convergence', rpc)
    assert [action for action, _ in calls] == ['resource_context']
    assert not b.resource_confirmations and b.submission_limits == original_limits


def test_return_to_modify_uses_last_valid_values_as_defaults():
    b, prompts, output, calls, rpc = dialog_run(['6', '16', '2', '', '', '3', '', '', '', '', '', '1'])
    result = confirm(b, 'test', 'si', 'convergence', rpc)
    assert result['resources']['nodes'] == 2 and result['resources']['cores_per_test'] == 16
    assert any(p.startswith('同时运行') and '[6]' in p for p in prompts)
    assert any(p.startswith('每组 MPI') and '[16]' in p for p in prompts)
    assert any(p.startswith('申请的物理节点数') and '[2]' in p for p in prompts)


def test_concurrency_cannot_exceed_minimum_authorized_cores():
    b, prompts, output, calls, rpc = dialog_run(['6', '2', '1', '1', '', '', '1'], limits={**LIMITS, 'cores': 2})
    assert confirm(b, 'test', 'si', 'convergence', rpc)['resources']['parallel_tests'] == 2
    assert any('至少需要 6 核' in line for line in output)
    assert prompts[2].startswith('同时运行')


def test_no_remaining_tests_stops_without_prompt_or_write():
    b, prompts, output, calls, rpc = dialog_run([], tests=0)
    with pytest.raises(ValueError, match='独立测试数量'):
        confirm(b, 'test', 'si', 'convergence', rpc)
    assert not prompts and [action for action, _ in calls] == ['resource_context']


def test_execution_plan_and_resources_are_described_in_words():
    from vasp_agent.permissions.submission_dialog import brief, describe_plan
    assert describe_plan({'tests': 6, 'compute': ['encut', 'mesh'], 'reuse': [], 'cross_cutoff': False,
                          'post_relax_checks': ['higher-cutoff', 'denser-mesh']}) == \
        '6 项测试（ENCUT 3 项 + k 网格 3 项）；优化后另做复核：更高截断能、更密网格'
    assert describe_plan({'tests': 4, 'compute': ['mesh'], 'reuse': ['encut'], 'cross_cutoff': True}) == \
        '4 项测试（k 网格 3 项 + 交叉复核 1 项）；复用 ENCUT（来源已通过）'
    assert brief(RESOURCES).startswith('allocation，') and '{' not in brief(RESOURCES)
