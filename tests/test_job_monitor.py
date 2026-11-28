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


@pytest.mark.parametrize('bad_value', [True, False, 59, 601, '60', 60.0])
def test_job_monitor_poll_interval_override_validation(tmp_path, bad_value):
    with pytest.raises(ValueError):
        JobMonitor(tmp_path, poll_interval_override=bad_value)


def test_job_monitor_poll_interval_override_accepts_bounds(tmp_path):
    assert JobMonitor(tmp_path, poll_interval_override=60)._poll_interval_override == 60
    assert JobMonitor(tmp_path, poll_interval_override=600)._poll_interval_override == 600


def test_job_monitor_poll_interval_override_tightens_without_mutating_disk_or_breaking_backoff(tmp_path):
    now = [1000.0]
    seed = JobMonitor(tmp_path, clock=lambda: now[0])
    seed.register('hpc', 'target', 'scf', '101', interval_seconds=300)
    seed.register('other', 'unrelated', 'scf', '202', interval_seconds=300)
    seed.register('hpc', 'target', 'bands', '303', interval_seconds=60)

    mon = JobMonitor(
        tmp_path,
        clock=lambda: now[0],
        poll_interval_override=120,
        poll_filter=lambda t: t['job_name'] == 'target' and t['phase'] == 'scf',
    )
    # Tightening override=60 on a 300s task; non-matching tasks keep original schedule.
    mon_tight = JobMonitor(
        tmp_path,
        clock=lambda: now[0],
        poll_interval_override=60,
        poll_filter=lambda t: t['job_name'] == 'target' and t['phase'] == 'scf',
    )
    # Also verify override never lengthens an already shorter interval (60s task with 120s override).
    mon_no_lengthen = JobMonitor(
        tmp_path,
        clock=lambda: now[0],
        poll_interval_override=120,
        poll_filter=lambda t: t['job_id'] == '303',
    )
    status_data = mon_no_lengthen.status()
    short_task = next(t for t in status_data['tasks'] if t['job_id'] == '303')
    assert mon_no_lengthen._effective_due(short_task) == 1060.0

    # At t=1050, _compute_wait reflects tightened due (1060 - 1050 = 10s).
    now[0] = 1050.0
    assert mon_tight._compute_wait(mon_tight.status()) == pytest.approx(10.0)

    calls = []
    should_fail = [False]

    def poll(target):
        calls.append(target['job_id'])
        if should_fail[0]:
            raise RuntimeError('transient ssh timeout')
        return result()

    mon_tight._poller = poll
    # At t=1060: matching 300s task is due via 60s override; 303 is due at 60s naturally; 202 (300s) is NOT due.
    now[0] = 1060.0
    polled = mon_tight._check(False)
    assert [e['job_id'] for e in polled] == ['101', '303']
    assert '202' not in calls
    # Disk interval_seconds remains untouched (300s).
    persisted = {t['job_id']: t for t in JobMonitor(tmp_path).status()['tasks']}
    assert persisted['101']['interval_seconds'] == 300
    assert persisted['101']['next_due_at'] == pytest.approx(1360.0)

    # Now induce 2 consecutive errors on 101 and verify exponential backoff (60s -> 120s).
    mon_only_101 = JobMonitor(
        tmp_path,
        clock=lambda: now[0],
        poll_interval_override=60,
        poll_filter=lambda t: t['job_id'] == '101',
    )
    mon_only_101._poller = poll
    should_fail[0] = True
    calls.clear()
    now[0] = 1120.0  # 1060 + 60 -> due for 101 (and 303); 101 fails -> err_cnt=1, next backoff 60s (1180)
    mon_only_101._check(False)
    assert '101' in calls

    calls.clear()
    now[0] = 1180.0  # 1120 + 60 -> due for 101; fails again -> err_cnt=2, next backoff 120s (1300)
    mon_only_101._check(False)
    assert '101' in calls
    persisted_err = {t['job_id']: t for t in JobMonitor(tmp_path).status()['tasks']}
    assert persisted_err['101']['error_count'] == 2
    assert persisted_err['101']['interval_seconds'] == 300

    calls.clear()
    now[0] = 1240.0  # Only 60s after 1180: must NOT poll 101 because backoff is 120s (due at 1300)
    mon_only_101._check(False)
    assert '101' not in calls

    calls.clear()
    now[0] = 1300.0  # 1180 + 120: now due
    mon_only_101._check(False)
    assert '101' in calls
    assert mon._poll_interval_override == 120


def test_job_monitor_check_once_task_filter_polls_only_matching(tmp_path):
    m, _ = fixture_monitor(tmp_path)
    m.register('hpc', 'job-a', 'scf', '10')
    m.register('hpc', 'job-b', 'scf', '20')
    calls = []
    m._poller = lambda target: calls.append(target['job_name']) or result()
    events = m.check_once(task_filter=lambda t: t['job_name'] == 'job-a')
    assert calls == ['job-a']
    assert [e['job_name'] for e in events] == ['job-a']
    tasks = {t['job_name']: t for t in m.status()['tasks']}
    assert tasks['job-a']['state'] == 'RUNNING'
    assert tasks['job-b']['state'] == 'UNQUERIED'


def test_service_watch_active_jobs_exposes_enabled_field(tmp_path):
    from vasp_agent.control import service_watch

    m, _ = fixture_monitor(tmp_path)
    m.register('hpc', 'si', 'scf', '55')
    jobs, monitor_enabled = service_watch._read_active_jobs(m)
    assert monitor_enabled is True
    assert len(jobs) == 1
    assert jobs[0]['enabled'] is True

