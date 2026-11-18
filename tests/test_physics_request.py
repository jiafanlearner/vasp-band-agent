import json
import pytest
from vasp_agent.chat.ai_agent import Toolbox
from vasp_agent.workflow import stage_gate as gate
from vasp_agent.workflow.vasp_band_agent import ToolError
from test_stage_gate import staged


def box_for(staged, answers):
    box = Toolbox(staged.parent.parent)
    replies = iter(answers)
    box.plan_reader = lambda _: next(replies)
    box.plan_output = lambda _: None
    return box


def test_operator_confirmation_and_retry_without_prompt(staged):
    box = box_for(staged, ['1'])
    args = dict(server=None, job_name='si', proposed_goal='非磁性 PBE 对照')
    result = box.call('request_physics_confirmation', args)
    assert result['ok'], result
    assert result['result']['confirmed']
    assert not result['result']['submission_authorized']
    before = (staged / 'physics-model.json').read_bytes()
    assert gate.physics(staged)['goal'] == args['proposed_goal']
    # The menu that ran was the in-chat one, so that is what the record names.
    assert gate.physics(staged)['confirmed_by'] == 'operator_chat_menu'
    assert box.call('request_physics_confirmation', args)['result']['status'] == 'already_confirmed'
    assert (staged / 'physics-model.json').read_bytes() == before
    assert not box.allow_submit


@pytest.mark.parametrize('answer', ['', 'q', '3', 'invalid'])
def test_cancel_never_writes(staged, answer):
    box = box_for(staged, [answer])
    result = box.request_physics_confirmation(None, 'si', 'control')
    assert result['status'] == 'cancelled'
    assert not (staged / 'physics-model.json').exists()


def test_operator_edits_goal(staged):
    box = box_for(staged, ['2', '使用者自己的对照目标', '确认'])
    box.request_physics_confirmation(None, 'si', 'AI 建议')
    assert gate.physics(staged)['goal'] == '使用者自己的对照目标'


def test_noninteractive_and_eof_do_not_confirm(staged):
    box = Toolbox(staged.parent.parent)
    assert box.request_physics_confirmation(None, 'si', 'control')['status'] == 'interaction_required'
    def eof(_):
        raise EOFError()
    box.plan_reader = eof
    box.plan_output = lambda _: None
    assert box.request_physics_confirmation(None, 'si', 'control')['status'] == 'cancelled'
    assert not (staged / 'physics-model.json').exists()


def test_remote_menu_only_writes_after_operator_choice(tmp_path, monkeypatch):
    from vasp_agent.remote.remote_compute import RemoteCompute
    calls = []
    monkeypatch.setattr(RemoteCompute, '__init__', lambda *a: None)
    def remote(self, action, job, **kwargs):
        calls.append((action, job, kwargs))
        if action == 'stage_context':
            raise ToolError('PHYSICS_CONFIRMATION_REQUIRED', 'missing')
        return {'goal': kwargs['goal']}
    monkeypatch.setattr(RemoteCompute, 'call', remote)
    box = Toolbox(tmp_path)
    box.plan_output = lambda _: None
    def choose(_):
        assert [c[0] for c in calls] == ['stage_context']
        return '1'
    box.plan_reader = choose
    result = box.request_physics_confirmation('server', 'job', 'control')
    assert result['confirmed']
    assert [c[0] for c in calls] == ['stage_context', 'confirm_physics']
