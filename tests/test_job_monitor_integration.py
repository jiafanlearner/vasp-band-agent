"""Confirmed submission registration and slash controls; no actual HPC traffic."""
import io
import json
from types import SimpleNamespace as NS

import pytest
from rich.console import Console

from vasp_agent.chat import ai_agent as ai
from vasp_agent.chat.monitor_commands import execute
from vasp_agent.remote.remote_compute import RemoteCompute
from vasp_agent.ui.terminal_ui import TerminalUI


def remote_box(tmp_path, monkeypatch, outcome=None, confirm=None):
    box = ai.Toolbox(tmp_path)
    box.allow_submit = True
    calls = []
    monkeypatch.setattr(RemoteCompute, '__init__', lambda *a: None)
    import vasp_agent.permissions.submission_dialog as dialog
    monkeypatch.setattr(dialog, 'confirm', lambda *a, **kw: confirm)
    def call(self, action, job, **params):
        calls.append((action, job, params))
        if action == 'submit_checked':
            if isinstance(outcome, Exception):
                raise outcome
            return outcome or {'status': 'submitted', 'job_id': '123', 'phase': params['phase']}
        if action == 'resource_context':
            return {'already_registered': True, 'status': 'submitted', 'job_id': '123', 'phase': params['phase']}
        pytest.fail('unexpected RPC ' + action)
    monkeypatch.setattr(RemoteCompute, 'call', call)
    return box, calls


def test_remote_submit_auto_register_and_ai_interval_without_model_request(tmp_path, monkeypatch):
    box, calls = remote_box(tmp_path, monkeypatch)
    box.recommend_job_check_interval('hpc', 'si', 'convergence', 10, '只有三组独立测试，适度缩短检查间隔。')
    result = box.remote_submit('hpc', 'si', 'convergence')
    assert result['job_id'] == '123' and result['monitoring']['registered']
    assert result['monitoring']['interval_seconds'] == 600
    assert [call[0] for call in calls] == ['submit_checked']
    assert box.get_job_monitor()['tasks'][0]['phase'] == 'convergence'
    assert box.submissions == 1


def test_monitor_failure_never_changes_success_receipt_or_repeats_sbatch(tmp_path, monkeypatch):
    box, calls = remote_box(tmp_path, monkeypatch)
    monkeypatch.setattr(box.monitor, 'register', lambda *a, **kw: (_ for _ in ()).throw(OSError('cannot save')))
    result = box.remote_submit('hpc', 'si', 'scf')
    assert result['status'] == 'submitted' and result['job_id'] == '123'
    assert not result['monitoring']['registered']
    assert len([call for call in calls if call[0] == 'submit_checked']) == 1
    assert not any(call[0] == 'resource_context' for call in calls)


def test_recovered_confirmed_receipt_registered_without_resubmit(tmp_path, monkeypatch):
    box, calls = remote_box(tmp_path, monkeypatch, OSError('lost response'))
    result = box.remote_submit('hpc', 'si', 'scf')
    assert result['record_recovered_after_error'] and result['submission_confirmed']
    assert result['monitoring']['registered']
    assert [call[0] for call in calls] == ['submit_checked', 'resource_context']


@pytest.mark.parametrize('data', [
    {'status': 'awaiting_resource_review', 'resources': {'nodes': 1}},
    {'status': 'submission_uncertain', 'job_id': None},
    {'status': 'submission_uncertain', 'job_id': '123'},
    {'status': 'submitted', 'job_id': '123', 'submission_confirmed': False},
    {'status': 'cancelled'},
])
def test_non_submission_results_do_not_start_watch(tmp_path, monkeypatch, data):
    box, calls = remote_box(tmp_path, monkeypatch, confirm=data)
    assert box.remote_submit('hpc', 'si', 'scf') == data
    assert calls == [] and box.monitor.status()['tasks'] == []
    assert box.submissions == 0


def test_existing_receipt_registration_is_idempotent_and_manual_setting_wins(tmp_path, monkeypatch):
    data = dict(already_registered=True, status='submitted', job_id='123', phase='scf')
    box, calls = remote_box(tmp_path, monkeypatch, confirm=data)
    box.monitor.configure(enabled=False, auto=False, interval_seconds=1200)
    box.recommend_job_check_interval('hpc', 'si', 'scf', 5, '短测试')
    for _ in range(2):
        result = box.remote_submit('hpc', 'si', 'scf')
        assert result['monitoring']['interval_seconds'] == 1200
        assert not result['monitoring']['enabled']
    assert calls == [] and len(box.monitor.status()['tasks']) == 1


def test_local_success_registration_outside_uncertain_receipt_handler(tmp_path, monkeypatch):
    script = tmp_path / 'source.slurm'
    script.write_text('#!/bin/bash\n#SBATCH --nodes=1\n#SBATCH --ntasks=8\n')
    folder = tmp_path / 'runs' / 'si'
    folder.mkdir(parents=True)
    (folder / 'config.json').write_text('{"walltime":"04:00:00"}')
    box = ai.Toolbox(tmp_path, submit_script=script, allow_submit=True)
    monkeypatch.setattr(ai.core, 'verify', lambda _: {'workflow': 'full'})
    import vasp_agent.permissions.submission_dialog as dialog
    monkeypatch.setattr(dialog, 'confirm', lambda *a, **kw: None)
    commands = []
    monkeypatch.setattr(ai.subprocess, 'run', lambda command, **kwargs: commands.append(command) or NS(stdout='123\n'))
    monkeypatch.setattr(box.monitor, 'register', lambda *a, **kw: (_ for _ in ()).throw(OSError('failure')))
    data = box.submit_job('si')
    assert data['job_id'] == '123' and not data['monitoring']['registered']
    assert json.loads((folder / 'submission.json').read_text())['status'] == 'submitted'
    assert len(commands) == 1


def controls(tmp_path):
    box = ai.Toolbox(tmp_path)
    box.monitor.register('hpc', 'si', 'scf', '123')
    output = []
    return NS(agent=NS(tools=box), output=output.append), box, output


def test_watch_commands_never_call_model_and_preserve_paused_setting(tmp_path):
    c, box, output = controls(tmp_path)
    for arg in ('', 'off', 'interval 20', 'auto'):
        execute(c, arg)
    assert not box.monitor.status()['enabled']
    assert box.monitor.status()['auto']
    execute(c, 'on')
    assert box.monitor.status()['enabled']
    execute(c, 'remove 123 hpc')
    assert box.monitor.status()['tasks'] == []
    assert not box.allow_submit
    assert any('默认间隔' in item for item in output)


@pytest.mark.parametrize('arg', ['interval 0', 'interval 1441', 'interval 1.5', 'bad', 'on extra'])
def test_watch_validation_does_not_mutate(tmp_path, arg):
    c, box, _ = controls(tmp_path)
    before = box.monitor.status()
    with pytest.raises(ValueError):
        execute(c, arg)
    assert box.monitor.status() == before


def test_watch_check_displays_once_and_background_notification_is_bounded(tmp_path):
    c, box, output = controls(tmp_path)
    notices = []
    box.monitor._notify = notices.append
    box.monitor._poller = lambda _: dict(state='COMPLETED', terminal=True, outcome='completed',
                                       workflow_status='awaiting_review', next_phase='bands')
    execute(c, 'check')
    # Shown once by the command; the event still goes out, flagged manual, so the
    # follow-up runs and the main loop does not print a second notice.
    assert len(output) == 2 and len(notices) == 1 and notices[0]['manual']
    assert '待审阅' in output[1]
    ui = TerminalUI('never')
    stream = io.StringIO()
    ui.console = lambda: Console(file=stream, width=200, color_system=None)
    ui.job_notification(box.monitor.status()['tasks'][0])
    text = stream.getvalue()
    assert len(text.splitlines()) <= 2 and 'JobID 123' in text
    assert '工作流已完成' not in text and ui.trace == []


def test_slash_catalog_discovers_watch_and_help(tmp_path):
    from test_chat_commands import setup
    from vasp_agent.ui.interactive_prompt import command_catalog
    c, output = setup(tmp_path)
    assert c.handle('/watch') == 'handled'
    assert '监控：已开启' in output[0]
    assert '/watch' in command_catalog()


def test_interactive_main_starts_and_stops_monitor_without_model_or_ssh(tmp_path, monkeypatch):
    from vasp_agent.models import api_client, model_config, model_adapters
    from vasp_agent.chat import chat_commands
    from vasp_agent.monitoring.job_monitor import JobMonitor
    from vasp_agent.monitoring import polling
    config = NS(api_key='test', api_key_env='TEST', model='test', protocol='chat_completions',
                identity=lambda: 'test', public=lambda: {'model': 'test'})
    monkeypatch.setattr(model_config, 'resolve_config', lambda *a, **kw: config)
    monkeypatch.setattr(api_client, 'create_client', lambda *a: NS())
    monkeypatch.setattr(model_adapters, 'adapter_for', lambda *a: NS())
    monkeypatch.setattr(ai, 'Agent', lambda *a, **kw: NS(usage={}))
    monkeypatch.setattr(chat_commands, 'Commands', lambda agent, *a, **kw:
                        NS(agent=agent, handle=lambda message: 'exit'))
    monkeypatch.setattr(TerminalUI, 'prompt', lambda self: '/exit')
    monkeypatch.setattr(polling, 'poll_job', lambda *a: pytest.fail('no registered targets'))
    started, stopped = [], []
    real_start, real_stop = JobMonitor.start, JobMonitor.stop
    def start(self, *args):
        started.append(self)
        return real_start(self, *args)
    def stop(self):
        stopped.append(self)
        return real_stop(self)
    monkeypatch.setattr(JobMonitor, 'start', start)
    monkeypatch.setattr(JobMonitor, 'stop', stop)
    monkeypatch.setattr(ai.sys, 'argv', ['ai_agent.py', '--workspace', str(tmp_path), '--model', 'test'])
    ai.main()
    assert len(started) == 1 and stopped == started
    assert not started[0].status()['running']


def fake_commands(name, history=()):
    switched = []
    c = NS(name=name, config=NS(identity=lambda: 'abcdef123456'), agent=NS(history=list(history)))
    def switch(target):
        switched.append(target)
        c.name, c.agent = target, NS(history=['old turn'])
    c.switch = switch
    return c, switched


def event(job, owner=None):
    return {'kind': 'terminal', 'job_name': job, 'job_id': '1', **({'conversation': owner + '-abcdef123456'} if owner else {})}


def test_followup_switches_an_empty_conversation_to_the_owner():
    c, switched = fake_commands('chat-new')
    deferred, shown = {}, []
    now = ai.route_followups(c, [event('la', 'chat-la')], deferred, shown.append)
    assert switched == ['chat-la'] and [e['job_name'] for e in now] == ['la'] and deferred == {}


def test_followup_waits_for_its_conversation_instead_of_hijacking():
    c, switched = fake_commands('chat-other', history=['busy'])
    deferred, shown = {}, []
    assert ai.route_followups(c, [event('la', 'chat-la'), event('si', 'chat-other')], deferred, shown.append)[0]['job_name'] == 'si'
    assert switched == [] and list(deferred) == ['chat-la']
    assert any('/sessions chat-la' in line for line in shown)
    c.name = 'chat-la'  # the operator opened it
    assert [e['job_name'] for e in ai.route_followups(c, [], deferred, shown.append)] == ['la'] and deferred == {}


def test_old_registrations_and_other_endpoints_follow_up_here():
    c, switched = fake_commands('chat-new')
    other = {**event('x'), 'conversation': 'chat-la-ffffffffffff'}
    now = ai.route_followups(c, [event('old'), other], {}, [].append)
    assert len(now) == 2 and switched == []


def test_registration_records_the_submitting_conversation(tmp_path, monkeypatch):
    box, _ = remote_box(tmp_path, monkeypatch)
    box.session_id = 'chat-la-abcdef123456'
    box.remote_submit('hpc', 'si', 'convergence')
    assert box.monitor.status()['tasks'][0]['conversation'] == 'chat-la-abcdef123456'
