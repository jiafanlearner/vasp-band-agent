"""Time and scheduler traffic are fake; no model, SSH or Slurm requests."""
from threading import Event

import pytest

from vasp_agent.monitoring.job_monitor import JobMonitor, MonitorPersistenceError


def fixture_monitor(tmp_path):
    now = [1000.0]
    monitor = JobMonitor(tmp_path, clock=lambda: now[0])
    return monitor, now


def result(state='RUNNING', terminal=False, **extra):
    return dict(state=state, terminal=terminal, outcome='completed' if terminal else None,
                workflow_status='awaiting_review' if terminal else 'running',
                next_phase='relaxation' if terminal else 'convergence', **extra)


def test_default_due_restart_and_idempotent_registration(tmp_path):
    m, now = fixture_monitor(tmp_path)
    assert m.status()['interval_seconds'] == 900
    one = m.register('hpc', 'si', 'convergence', '123')
    now[0] += 20
    assert m.register('hpc', 'si', 'convergence', '123') == one
    assert one['next_due_at'] == 1900 and one['interval_seconds'] == 900
    another = JobMonitor(tmp_path, clock=lambda: now[0])
    assert another.status()['tasks'] == [one]
    assert not another.status()['running']
    events, calls = [], []
    m._poller = lambda target: calls.append(target) or result()
    m._notify = events.append
    assert m._check(False) == [] and not calls
    now[0] = 1900
    assert m._check(False)[0]['changed']
    assert len(events) == 1 and calls[0]['job_id'] == '123'
    now[0] += 900
    assert not m._check(False)[0]['changed']
    assert len(events) == 1


def test_global_manual_overrides_ai_and_auto_keeps_disabled(tmp_path):
    m, now = fixture_monitor(tmp_path)
    assert m.register('hpc', 'si', 'scf', '1', interval_seconds=300)['interval_seconds'] == 300
    m.configure(enabled=False, auto=False, interval_seconds=1200)
    assert m.status()['tasks'][0]['interval_seconds'] == 1200
    assert m.register('hpc', 'si', 'bands', '2', interval_seconds=60)['interval_seconds'] == 1200
    m.configure(auto=True)
    assert not m.status()['enabled']
    assert [t['interval_seconds'] for t in m.status()['tasks']] == [300, 60]
    m._poller = lambda target: result()
    now[0] += 10000
    assert m._check(False) == []
    assert len(m.check_once()) == 2  # Explicit read works while paused.
    m.configure(enabled=True)
    assert m.status()['enabled']


def test_duration_heuristics_are_bounded_and_not_runtime_estimates(tmp_path):
    m, _ = fixture_monitor(tmp_path)
    assert m.register('hpc', 'one', 'scf', '1', resources={'walltime': '00:30:00'})['interval_seconds'] == 300
    assert m.register('hpc', 'two', 'scf', '2', resources={'walltime': '04:00:00'})['interval_seconds'] == 900
    assert m.register('hpc', 'three', 'scf', '3', resources={'walltime': '10:00:00'})['interval_seconds'] == 1800


def test_error_backoff_duplicate_suppression_and_recovery(tmp_path):
    m, now = fixture_monitor(tmp_path)
    m.register('hpc', 'si', 'scf', '123')
    events = []
    m._notify = events.append
    def fail(_):
        raise OSError('secret=do-not-persist')
    m._poller = fail
    now[0] += 900
    first = m._check(False)[0]
    assert first['kind'] == 'error' and not first['terminal']
    now[0] += 900
    second = m._check(False)[0]
    assert not second['changed'] and second['next_due_at'] - now[0] == 1800
    assert len(events) == 1 and 'do-not-persist' not in m.path.read_text(encoding='utf-8')
    m._poller = lambda _: result()
    now[0] += 1800
    assert m._check(False)[0]['kind'] == 'recovered'
    assert len(events) == 2


def test_terminal_only_stops_exact_phase_and_never_submits_next(tmp_path):
    m, now = fixture_monitor(tmp_path)
    m.register('hpc', 'si', 'convergence', '1')
    m.register('hpc', 'si', 'relaxation', '2')
    calls = []
    def poll(target):
        calls.append((target['job_id'], target['phase']))
        return result('COMPLETED', True) if target['job_id'] == '1' else result()
    m._poller = poll
    events = []
    m._notify = events.append
    now[0] += 900
    m._check(False)
    assert calls == [('1', 'convergence'), ('2', 'relaxation')]
    tasks = m.status()['tasks']
    assert not tasks[0]['active'] and tasks[1]['active']
    now[0] += 900
    m._check(False)
    assert calls[-1] == ('2', 'relaxation') and len(calls) == 3
    assert len(events) == 2


def test_multiple_instances_merge_and_worker_owner_prevents_duplicates(tmp_path):
    first = JobMonitor(tmp_path)
    second = JobMonitor(tmp_path)
    first.register('hpc', 'si', 'scf', '1')
    second.register('hpc', 'si', 'bands', '2')
    assert len(first.status()['tasks']) == 2
    assert first.start(lambda _: result(), lambda _: None)
    try:
        assert not second.start(lambda _: pytest.fail('duplicate poll'), lambda _: None)
        assert second.check_once() == []
    finally:
        first.stop()
        second.stop()
    assert not first.status()['running']


def test_background_stop_does_not_notify_after_exit(tmp_path):
    m, now = fixture_monitor(tmp_path)
    m.register('hpc', 'si', 'scf', '1')
    now[0] += 900
    started, release = Event(), Event()
    notices = []
    def poll(_):
        started.set()
        assert release.wait(timeout=3)
        return result()
    assert m.start(poll, notices.append)
    try:
        assert started.wait(timeout=2)
        m.stop()
    finally:
        release.set()
        m._thread.join(timeout=3)
    assert notices == []


@pytest.mark.parametrize('job_id', ['12;bad', '', None, '123_0'])
def test_invalid_ids_never_register(tmp_path, job_id):
    m = JobMonitor(tmp_path)
    with pytest.raises(ValueError):
        m.register('hpc', 'si', 'scf', job_id)
    assert not m.path.exists()


def test_corrupt_registry_preserved_and_multi_server_remove_requires_scope(tmp_path):
    m = JobMonitor(tmp_path)
    m.register('hpc', 'si', 'scf', '1')
    m.register('other', 'si', 'scf', '1')
    with pytest.raises(ValueError, match='多个服务器'):
        m.remove('1')
    assert m.remove('1', server='other')
    m.path.write_text('broken', encoding='utf-8')
    with pytest.raises(MonitorPersistenceError):
        m.configure(enabled=False)
    assert m.path.read_text() == 'broken'


def test_notification_failure_does_not_stop_other_jobs(tmp_path):
    m, now = fixture_monitor(tmp_path)
    m.register('hpc', 'si', 'scf', '1')
    m.register('hpc', 'si', 'bands', '2')
    m._poller = lambda _: result()
    m._notify = lambda _: (_ for _ in ()).throw(RuntimeError('broken screen'))
    now[0] += 900
    assert len(m._check(False)) == 2
    assert all(task['state'] == 'RUNNING' for task in m.status()['tasks'])


def test_status_line_for_the_prompt_bar():
    from types import SimpleNamespace as NS
    from vasp_agent.monitoring.job_monitor import status_line
    task = {'active': True, 'terminal': False, 'job_name': 'la2cuo4-03', 'phase': 'convergence',
            'state': 'RUNNING', 'next_due_at': 1_800_000_000, 'last_checked_at': 1_799_999_100, 'error_count': 0}
    monitor = NS(status=lambda: {'enabled': True, 'running': True, 'tasks': [task]})
    line = status_line(monitor)
    assert line.startswith('监控中 · la2cuo4-03（convergence） RUNNING') and '下次检查' in line and '上次' in line
    assert status_line(NS(status=lambda: {'enabled': False, 'running': True, 'tasks': [task]})).startswith('监控已暂停')
    assert status_line(NS(status=lambda: {'enabled': True, 'running': False, 'tasks': [task]})).startswith('监控未运行')
    assert status_line(NS(status=lambda: {'enabled': True, 'running': True, 'tasks': [{**task, 'terminal': True}]})) == ''
    assert '连续读取失败 2 次' in status_line(NS(status=lambda: {'enabled': True, 'running': True,
                                                         'tasks': [{**task, 'error_count': 2}]}))


def test_manual_check_still_reports_a_terminal_state_it_finds(tmp_path):
    m, now = fixture_monitor(tmp_path)
    m.register('hpc', 'la2cuo4', 'convergence', '71429780')
    events = []
    m._notify = events.append
    m._poller = lambda target: result()
    m.check_once()
    assert events == []  # a still-running job: the command's own output is enough
    m._poller = lambda target: result(state='FAILED', terminal=True)
    m.configure(enabled=False)  # even with background monitoring paused
    found = m.check_once()
    assert found[0]['kind'] == 'terminal' and not m.status()['tasks'][0]['active']
    assert len(events) == 1 and events[0]['manual'] and events[0]['state'] == 'FAILED'
    assert m.check_once() == []  # deactivated: not reported twice
