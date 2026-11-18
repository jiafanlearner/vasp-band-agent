"""Unified operator consent and defaults, with no model/SSH/scheduler traffic."""
import json

import pytest

from vasp_agent.chat.ai_agent import Toolbox
from vasp_agent.permissions.resource_menu import resource_menu, save_defaults, load_defaults, parse_custom
from test_submission_permissions import commands
from vasp_agent.common import agent_paths

RES = dict(parallel_tests=6, cores_per_test=16, nodes=2, partition='compute', walltime='04:00:00')


def menu(tmp_path, answers, **kwargs):
    inputs = iter(answers)
    output, prompts = [], []
    def read(prompt):
        prompts.append(prompt)
        return next(inputs)
    result = resource_menu(tmp_path, 'test', kwargs.pop('phase', 'convergence'),
                           kwargs.pop('current', RES), read, output.append, **kwargs)
    return result, output, prompts


def test_default_persistence_never_grants_or_restores_permission(tmp_path):
    result, _, prompts = menu(tmp_path, ['1'])
    assert result == RES and len(prompts) == 1
    assert load_defaults(tmp_path, 'test', 'convergence') == RES
    box = Toolbox(tmp_path)
    assert not box.allow_submit and box.submission_limits is None
    assert box.submission_target is None
    saved = json.loads(next(agent_paths.store_dir(tmp_path, 'submission_defaults').glob('*.json')).read_text())
    assert set(saved) == set(RES)


def test_saved_resources_take_priority_but_only_current_tests_are_used(tmp_path):
    save_defaults(tmp_path, 'test', 'convergence', RES)
    result, output, prompts = menu(tmp_path, ['1'], current={**RES, 'cores_per_test': 8}, tests=3, job='si')
    assert result == {**RES, 'parallel_tests': 3}
    assert any('用户已保存' in text for text in output)
    assert len(prompts) == 1 and load_defaults(tmp_path, 'test', 'convergence') == RES


def test_defaults_are_server_and_stage_scoped(tmp_path):
    save_defaults(tmp_path, 'test', 'convergence', RES)
    assert load_defaults(tmp_path, 'other', 'convergence') is None
    assert load_defaults(tmp_path, 'test', 'relaxation') is None
    single = {**RES, 'parallel_tests': 1}
    save_defaults(tmp_path, 'test', 'relaxation', single)
    assert load_defaults(tmp_path, 'test', 'bands') == single


def test_custom_input_is_previewed_before_explicit_confirmation(tmp_path):
    result, output, prompts = menu(tmp_path, ['并发=3 每组=8 节点=2 时限=02:00:00', '2'], job='si')
    expected = {**RES, 'parallel_tests': 3, 'cores_per_test': 8, 'walltime': '02:00:00'}
    assert result == expected and len(prompts) == 2
    assert any('操作者自定义，待确认' in text for text in output)
    assert load_defaults(tmp_path, 'test', 'convergence') == expected


def test_last_custom_option_and_use_once_does_not_save(tmp_path):
    result, _, prompts = menu(tmp_path, ['5', '并发=2 每组=8 节点=1', '1'], job='si')
    assert result['parallel_tests'] == 2 and len(prompts) == 3
    assert not agent_paths.store_dir(tmp_path, 'submission_defaults').exists()


@pytest.mark.parametrize('bad', ['grant=true', '并发=2 parallel_tests=3', '节点=96', '分区=compute;sbatch', '每组=-1'])
def test_invalid_custom_values_do_not_change_candidate_or_write(tmp_path, bad):
    result, output, _ = menu(tmp_path, [bad, '1'], job='si')
    assert result == RES
    assert any('参数未应用' in text for text in output)
    assert not agent_paths.store_dir(tmp_path, 'submission_defaults').exists()


@pytest.mark.parametrize('answer', ['', 'q', '4'])
def test_submission_cancel_never_persists(tmp_path, answer):
    with pytest.raises(ValueError, match='取消'):
        menu(tmp_path, [answer], job='si')
    assert not agent_paths.store_dir(tmp_path, 'submission_defaults').exists()


def test_both_slash_aliases_use_the_same_offline_defaults_menu(tmp_path):
    for command in ('/submit', '/permissions'):
        # Bare command first asks what is meant; 2 is the defaults editor.
        c = commands(tmp_path, ['2', '1', '1', '1'])
        c.agent.ask = lambda *a: pytest.fail('must not call model')
        assert c.handle(command) == 'handled'
        assert load_defaults(tmp_path, 'test', 'convergence')['cores_per_test'] == 8
        assert not c.agent.tools.allow_submit


def test_poisoned_defaults_do_not_restore_authorization(tmp_path):
    save_defaults(tmp_path, 'test', 'convergence', RES)
    path = next(agent_paths.store_dir(tmp_path, 'submission_defaults').glob('*.json'))
    path.write_text(json.dumps({**RES, 'allow_submit': True}))
    result, output, _ = menu(tmp_path, ['1'], job='si')
    assert result == RES and any('无效或不可读' in line for line in output)
    assert not Toolbox(tmp_path).allow_submit


def test_new_submission_menu_grants_only_named_target_and_does_not_reask_after_review(tmp_path, monkeypatch):
    from vasp_agent.remote.remote_compute import RemoteCompute
    c = commands(tmp_path, [])
    box = c.agent.tools
    save_defaults(tmp_path, 'test', 'convergence', RES)
    state = {'signature': 'a', 'resources': {**RES, 'parallel_tests': 1}, 'execution_plan': {'tests': 6}}
    calls, prompts = [], []
    def read(prompt):
        assert not box.allow_submit
        prompts.append(prompt)
        return '1'
    box.plan_reader = read
    def rpc(self, action, job, **kwargs):
        calls.append(action)
        if action == 'resource_context':
            return state.copy()
        if action == 'configure_and_submit':
            assert box.submission_target == ('test', 'si', 'convergence')
            state.update(signature='b', resources=kwargs['resources'])
            return {'signature': 'b', 'changed': True, 'resources': kwargs['resources'],
                    'submission': None, 'not_submitted': 'needs_review'}
        assert action == 'submit_checked'
        return {'job_id': 'mock-only', 'status': 'submitted'}
    monkeypatch.setattr(RemoteCompute, 'call', rpc)
    result = box.call('request_submission', {'server': 'test', 'job_name': 'si', 'phase': 'convergence'})
    assert result['ok'] and result['result']['status'] == 'awaiting_resource_review'
    assert 'submit_checked' not in calls
    assert box.submission_limits == dict(server='test', partition='compute', nodes=2, cores=96, walltime='04:00:00')
    with pytest.raises(ValueError):
        box.remote_submit('test', 'other', 'convergence')
    with pytest.raises(ValueError):
        box.remote_submit('test', 'si', 'relaxation')
    assert box.remote_submit('test', 'si', 'convergence')['job_id'] == 'mock-only'
    assert len(prompts) == 1 and calls.count('submit_checked') == 1 and not box.allow_submit


def test_request_menu_requires_operator_not_model_parameters(tmp_path, monkeypatch):
    box = Toolbox(tmp_path)
    monkeypatch.setattr('vasp_agent.remote.remote_compute.RemoteCompute.call', lambda *a, **k: pytest.fail('no remote call'))
    result = box.call('request_submission', dict(server='test', job_name='si', phase='convergence'))
    assert not result['ok'] and not box.allow_submit
    assert not box.call('request_submission', dict(server='test', job_name='si', phase='convergence', confirmed=True))['ok']


def test_saved_values_do_not_bypass_an_existing_grant_limit(tmp_path):
    save_defaults(tmp_path, 'test', 'convergence', RES)
    limits = dict(nodes=1, cores=8, partition='compute', walltime='04:00:00')
    result, output, prompts = menu(tmp_path, ['1', '并发=1 每组=8 节点=1', '1'], job='si', limits=limits)
    assert result['parallel_tests'] == 1
    assert len(prompts) == 3 and any('不可确认' in text for text in output)


def test_unknown_parameter_parser_rejects_execution_text():
    with pytest.raises(ValueError):
        parse_custom('并发=6;rm', RES)


def test_unified_confirmation_does_not_bypass_review_and_session_switch_revokes(tmp_path, monkeypatch):
    from vasp_agent.remote.remote_compute import RemoteCompute
    c = commands(tmp_path, ['1'])
    box = c.agent.tools
    save_defaults(tmp_path, 'test', 'convergence', RES)
    calls = []
    def rpc(self, action, job, **kwargs):
        calls.append(action)
        if action == 'resource_context':
            return {'signature': 'a', 'resources': RES, 'execution_plan': {'tests': 6}}
        if action == 'configure_and_submit':
            # Written unchanged, then refused before sbatch, all in the same call.
            return {'signature': 'a', 'changed': False, 'resources': RES,
                    'submission': {'ready': False, 'submission_not_attempted': True, 'error': '阶段审阅未通过'}}
        pytest.fail('No submission before review')
    monkeypatch.setattr(RemoteCompute, 'call', rpc)
    with pytest.raises(ValueError, match='审阅未通过'):
        box.request_submission('test', 'si', 'convergence')
    assert 'submit' not in calls and box.submissions == 0
    assert box.submission_target == ('test', 'si', 'convergence')
    c.handle('/new other')
    assert not box.allow_submit and box.submission_target is None
    assert load_defaults(tmp_path, 'test', 'convergence') == RES


def test_cancel_unified_tool_does_not_grant_or_write(tmp_path, monkeypatch):
    from vasp_agent.remote.remote_compute import RemoteCompute
    c = commands(tmp_path, ['q'])
    box = c.agent.tools
    calls = []
    def rpc(self, action, job, **kwargs):
        calls.append(action)
        assert action == 'resource_context'
        return {'signature': 'a', 'resources': RES}
    monkeypatch.setattr(RemoteCompute, 'call', rpc)
    with pytest.raises(ValueError, match='取消'):
        box.request_submission('test', 'si', 'convergence')
    assert calls == ['resource_context'] and not box.allow_submit
    assert not box.resource_confirmations and not agent_paths.store_dir(tmp_path, 'submission_defaults').exists()


def test_authorized_request_reads_resources_once_and_never_reopens_menu(tmp_path, monkeypatch):
    from vasp_agent.remote.remote_compute import RemoteCompute
    c = commands(tmp_path, [])
    box = c.agent.tools
    key = ('test', 'si', 'convergence')
    box.allow_submit, box.submission_target = True, key
    box.submission_limits = dict(server='test', partition='compute', nodes=2, cores=96, walltime='04:00:00')
    box.resource_confirmations[key] = 'a'
    box.plan_reader = lambda prompt: pytest.fail('operator menu must not reopen')
    signatures, calls = iter(['a', 'changed-after-confirm']), []
    def rpc(self, action, job, **kwargs):
        calls.append(action)
        if action == 'resource_context':
            return {'signature': next(signatures), 'resources': {**RES, 'parallel_tests': 1}, 'execution_plan': {'tests': 6}}
        assert action == 'submit_checked'
        return {'job_id': 'mock-only', 'status': 'submitted'}
    monkeypatch.setattr(RemoteCompute, 'call', rpc)
    assert box.request_submission('test', 'si', 'convergence')['job_id'] == 'mock-only'
    assert calls == ['resource_context', 'submit_checked']


@pytest.mark.parametrize('text', ['并发=1 每组=32 节点=1', '并发＝1　每组＝３２　节点＝1', '并发=1，每组=32，节点=1',
                                  '每组=32 时限=04：00：00'])
def test_custom_input_accepts_full_width_ime_text(text):
    from vasp_agent.permissions.resource_menu import parse_custom
    base = dict(parallel_tests=1, cores_per_test=8, nodes=1, partition='p', walltime='04:00:00')
    parsed = parse_custom(text, base)
    assert parsed['cores_per_test'] == 32 and parsed['walltime'] == '04:00:00' and parsed['nodes'] == 1


def test_custom_input_echoes_what_was_recognized(tmp_path):
    from vasp_agent.permissions.resource_menu import resource_menu
    shown, answers = [], iter(['并发＝1 每组＝32 节点＝1', '1'])
    base = dict(parallel_tests=1, cores_per_test=8, nodes=1, partition='p', walltime='04:00:00')
    result = resource_menu(tmp_path, 'hpc', 'bands', base, lambda _: next(answers), shown.append, tests=1, job='la')
    assert result['cores_per_test'] == 32
    assert any(line.startswith('已识别：') and '每组=32' in line for line in shown)
