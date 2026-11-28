"""Offline TDD regression suite for event-driven `vasp-ctl watch wait` (including Windows real OS FileLock)."""
from __future__ import annotations

import json
import threading
import time

import pytest

from vasp_agent.common import agent_paths
from vasp_agent.common.file_lock import FileLock
from vasp_agent.control import cli as control_cli
from vasp_agent.control import watch_wait
from vasp_agent.monitoring import event_outbox
from vasp_agent.monitoring.job_monitor import JobMonitor


def _poll_result(state: str = 'RUNNING', terminal: bool = False, **extra):
    return {
        'state': state,
        'terminal': terminal,
        'outcome': 'completed' if terminal else None,
        'workflow_status': 'awaiting_review' if terminal else 'running',
        'next_phase': 'bands' if terminal else 'scf',
        **extra,
    }


def _sample_event(**overrides):
    base = {
        'server': 'hpc',
        'job_name': 'si-job',
        'phase': 'scf',
        'job_id': '101',
        'kind': 'terminal',
        'changed': True,
        'state': 'COMPLETED',
        'terminal': True,
        'outcome': 'completed',
        'workflow_status': 'awaiting_review',
        'next_phase': 'bands',
        'error': None,
        'error_count': 0,
        'message': '作业已结束，等待审阅。',
    }
    base.update(overrides)
    return base


def _seed_task_fields(monitor: JobMonitor, job_id: str, **updates) -> None:
    with monitor._transaction(write=True) as data:
        for task in data['tasks']:
            if task['job_id'] == job_id:
                task.update(updates)


def _scaled_clock(start: float = 1000.0, scale: float = 10000.0):
    t0 = time.monotonic()
    return lambda: start + (time.monotonic() - t0) * scale


def test_watch_wait_rejects_checker_and_invalid_arguments(tmp_path, capsys):
    rc = control_cli.main(['--workspace', str(tmp_path), '--role', 'checker', 'watch', 'wait', '--json'])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload['ok'] is False and payload['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'

    for argv in (
        ['--workspace', str(tmp_path), 'watch', 'wait', '--poll-interval', '59', '--json'],
        ['--workspace', str(tmp_path), 'watch', 'wait', '--poll-interval', '601', '--json'],
        ['--workspace', str(tmp_path), 'watch', 'wait', '--max-consecutive-errors', '0', '--json'],
        ['--workspace', str(tmp_path), 'watch', 'wait', '--max-consecutive-errors', '51', '--json'],
        ['--workspace', str(tmp_path), 'watch', 'wait', '--max-seconds', '0', '--json'],
        ['--workspace', str(tmp_path), 'watch', 'wait', '--max-seconds=nan', '--json'],
        ['--workspace', str(tmp_path), 'watch', 'wait', '--server', 'bad/srv', '--json'],
        ['--workspace', str(tmp_path), 'watch', 'wait', '--job-name', 'bad;job', '--json'],
    ):
        rc = control_cli.main(argv)
        assert rc == 2
        err = json.loads(capsys.readouterr().out)
        assert err['ok'] is False and err['error_code'] == 'INVALID_ARGUMENTS'

    # Direct Python API must reject bool (isinstance(True, int) is True) and bad phase.
    for kwargs in (
        {'poll_interval': True},
        {'poll_interval': False},
        {'max_consecutive_errors': True},
        {'max_seconds': True},
        {'phase': 'not-a-phase'},
    ):
        rc = watch_wait.run_wait(tmp_path, as_json=True, **kwargs)
        assert rc == 2
        err = json.loads(capsys.readouterr().out)
        assert err['ok'] is False and err['error_code'] == 'INVALID_ARGUMENTS'


def test_watch_wait_immediate_return_on_existing_terminal_or_no_active_jobs_or_monitor_disabled(tmp_path, capsys):
    # 1. Empty workspace -> immediate no_active_jobs without creating .agent/ files.
    rc = watch_wait.run_wait(tmp_path, as_json=True)
    assert rc == 0
    body = json.loads(capsys.readouterr().out)['result']
    assert body['reason'] == 'no_active_jobs' and body['mode'] == 'immediate'
    assert not agent_paths.agent_root(tmp_path).exists()

    # 2. Existing pending terminal event -> immediate event wakeup.
    ws_term = tmp_path / 'term'
    ws_term.mkdir()
    ev = event_outbox.append_event(ws_term, _sample_event(job_id='101'))
    rc = watch_wait.run_wait(ws_term, server='hpc', job_name='si-job', phase='scf', as_json=True)
    assert rc == 0
    body = json.loads(capsys.readouterr().out)['result']
    assert body['reason'] == 'event' and body['mode'] == 'immediate'
    assert [e['event_id'] for e in body['wake_events']] == [ev['event_id']]

    # 3. Globally paused monitor (/watch off) -> immediate monitor_disabled without polling.
    ws_off = tmp_path / 'off'
    ws_off.mkdir()
    m_off = JobMonitor(ws_off)
    m_off.register('hpc', 'si-job', 'scf', '201')
    m_off.configure(enabled=False)
    rc = watch_wait.run_wait(
        ws_off,
        poller=lambda _t: pytest.fail('must not poll when monitor is globally disabled'),
        as_json=True,
    )
    assert rc == 0
    body = json.loads(capsys.readouterr().out)['result']
    assert body['reason'] == 'monitor_disabled' and body['mode'] == 'immediate'

    # 4. Per-task disabled -> immediate monitor_disabled (also check human text mentions /watch on).
    m_off.configure(enabled=True)
    _seed_task_fields(m_off, '201', enabled=False)
    rc = watch_wait.run_wait(
        ws_off,
        poller=lambda _t: pytest.fail('must not poll when matching tasks are disabled'),
        as_json=False,
    )
    assert rc == 0
    out_text = capsys.readouterr().out
    assert '/watch on' in out_text


def test_watch_wait_ignores_transient_error_and_wakes_on_terminal(tmp_path, capsys):
    clock = _scaled_clock(start=1000.0, scale=20000.0)
    m = JobMonitor(tmp_path, clock=clock)
    m.register('hpc', 'si-job', 'scf', '301', interval_seconds=60)
    _seed_task_fields(m, '301', next_due_at=0.0)

    step = [0]

    def poll(_task):
        step[0] += 1
        if step[0] == 1:
            raise TimeoutError('transient ssh timeout')
        if step[0] == 2:
            return _poll_result(state='RUNNING', terminal=False)
        return _poll_result(state='COMPLETED', terminal=True)

    rc = watch_wait.run_wait(
        tmp_path,
        server='hpc',
        job_name='si-job',
        phase='scf',
        max_consecutive_errors=3,
        max_seconds=5.0,
        max_idle_wait=0.02,
        observer_interval=0.02,
        clock=clock,
        poller=poll,
        as_json=True,
    )
    assert rc == 0
    raw = capsys.readouterr().out.strip()
    # Must be silent during wait: stdout holds exactly one JSON payload.
    payload = json.loads(raw)
    assert payload['ok'] is True
    res = payload['result']
    assert res['reason'] == 'event' and res['mode'] == 'owner'
    assert res['daemon_detected'] is False
    assert [e['kind'] for e in res['wake_events']] == ['terminal']
    assert step[0] == 3
    assert event_outbox.worker_lock_held(tmp_path) is False


def test_watch_wait_recovered_only_suppresses_same_job_identity(tmp_path, capsys):
    # Case A: [error(jobA, 101), recovered(jobA, 101)] -> old error is suppressed even with wake_on_error=True.
    ws_a = tmp_path / 'same-job'
    ws_a.mkdir()
    m_a = JobMonitor(ws_a)
    m_a.register('hpc', 'jobA', 'scf', '101', interval_seconds=900)
    event_outbox.append_event(
        ws_a, _sample_event(job_name='jobA', job_id='101', kind='error', state='RUNNING', terminal=False, error='Timeout', error_count=1),
    )
    event_outbox.append_event(
        ws_a, _sample_event(job_name='jobA', job_id='101', kind='recovered', state='RUNNING', terminal=False, error=None, error_count=0),
    )
    rc_a = watch_wait.run_wait(
        ws_a,
        wake_on_error=True,
        max_seconds=0.15,
        max_idle_wait=0.05,
        observer_interval=0.05,
        poller=lambda _t: _poll_result(),
        as_json=True,
    )
    assert rc_a == 0
    res_a = json.loads(capsys.readouterr().out)['result']
    assert res_a['reason'] == 'timeout' and res_a['wake_events'] == []

    # Case B: [error(jobB, 202), recovered(jobA, 101)] -> jobA recovery must NEVER suppress jobB's error.
    ws_b = tmp_path / 'cross-job'
    ws_b.mkdir()
    m_b = JobMonitor(ws_b)
    m_b.register('hpc', 'jobB', 'scf', '202', interval_seconds=900)
    err_b = event_outbox.append_event(
        ws_b, _sample_event(job_name='jobB', job_id='202', kind='error', state='RUNNING', terminal=False, error='Timeout', error_count=1),
    )
    event_outbox.append_event(
        ws_b, _sample_event(job_name='jobA', job_id='101', kind='recovered', state='RUNNING', terminal=False, error=None, error_count=0),
    )
    rc_b = watch_wait.run_wait(
        ws_b,
        wake_on_error=True,
        max_seconds=2.0,
        poller=lambda _t: pytest.fail('should wake immediately on unrecovered jobB error'),
        as_json=True,
    )
    assert rc_b == 0
    res_b = json.loads(capsys.readouterr().out)['result']
    assert res_b['reason'] == 'event' and res_b['mode'] == 'immediate'
    assert [e['event_id'] for e in res_b['wake_events']] == [err_b['event_id']]

    # Case C: [error(jobA, 101), terminal(jobA, 101)] -> terminal supersedes old error under wake_on_error=True.
    ws_c = tmp_path / 'term-supersedes-error'
    ws_c.mkdir()
    event_outbox.append_event(
        ws_c, _sample_event(job_name='jobA', job_id='101', kind='error', state='RUNNING', terminal=False, error='Timeout', error_count=1),
    )
    term_a = event_outbox.append_event(
        ws_c, _sample_event(job_name='jobA', job_id='101', kind='terminal', state='COMPLETED', terminal=True),
    )
    rc_c = watch_wait.run_wait(ws_c, wake_on_error=True, as_json=True)
    assert rc_c == 0
    res_c = json.loads(capsys.readouterr().out)['result']
    assert [e['event_id'] for e in res_c['wake_events']] == [term_a['event_id']]


def test_watch_wait_wake_on_error_wakes_on_single_error(tmp_path, capsys):
    clock = _scaled_clock(start=1000.0, scale=20000.0)
    m = JobMonitor(tmp_path, clock=clock)
    m.register('hpc', 'si-job', 'scf', '350', interval_seconds=60)
    _seed_task_fields(m, '350', next_due_at=0.0)

    def fail_once(_task):
        raise RuntimeError('ssh unreachable')

    rc = watch_wait.run_wait(
        tmp_path,
        wake_on_error=True,
        max_consecutive_errors=3,
        max_seconds=5.0,
        max_idle_wait=0.02,
        observer_interval=0.02,
        clock=clock,
        poller=fail_once,
        as_json=True,
    )
    assert rc == 0
    res = json.loads(capsys.readouterr().out)['result']
    assert res['reason'] == 'event' and res['mode'] == 'owner'
    assert [e['kind'] for e in res['wake_events']] == ['error']


def test_watch_wait_wakes_on_same_error_deduped_threshold_via_registry_error_count(tmp_path, capsys):
    clock = _scaled_clock(start=1000.0, scale=30000.0)
    m = JobMonitor(tmp_path, clock=clock)
    m.register('hpc', 'si-job', 'scf', '401', interval_seconds=60)
    _seed_task_fields(m, '401', next_due_at=0.0)

    calls = [0]

    def always_same_error(_task):
        calls[0] += 1
        raise RuntimeError('ssh timeout')

    rc = watch_wait.run_wait(
        tmp_path,
        max_consecutive_errors=3,
        max_seconds=5.0,
        max_idle_wait=0.02,
        observer_interval=0.02,
        clock=clock,
        poller=always_same_error,
        as_json=True,
    )
    assert rc == 0
    res = json.loads(capsys.readouterr().out)['result']
    assert res['reason'] == 'error_threshold_reached' and res['mode'] == 'owner'
    assert calls[0] >= 3
    # Outbox deduplicated identical error to 1 row, while registry tracked error_count >= 3.
    assert len(res['pending_events']) == 1
    assert res['error_jobs'][0]['job_id'] == '401'
    assert res['error_jobs'][0]['error_count'] >= 3


def test_watch_wait_owner_reentry_probes_once_and_breaks_zero_ms_loop(tmp_path, capsys):
    # Subcase A: pre-seeded error_count=3, cluster recovered -> probe resets error_count and waits for terminal.
    ws_rec = tmp_path / 'recovered'
    ws_rec.mkdir()
    clock_a = _scaled_clock(start=1000.0, scale=20000.0)
    m_a = JobMonitor(ws_rec, clock=clock_a)
    m_a.register('hpc', 'si-job', 'scf', '501', interval_seconds=60)
    _seed_task_fields(m_a, '501', error='RuntimeError', error_count=3, next_due_at=0.0)
    event_outbox.append_event(
        ws_rec, _sample_event(job_id='501', kind='error', state='UNQUERIED', terminal=False, error='RuntimeError', error_count=1),
    )

    calls_a = [0]

    def poll_recover_then_complete(_task):
        calls_a[0] += 1
        if calls_a[0] == 1:
            return _poll_result(state='RUNNING', terminal=False)
        return _poll_result(state='COMPLETED', terminal=True)

    rc_a = watch_wait.run_wait(
        ws_rec,
        max_consecutive_errors=3,
        max_seconds=5.0,
        max_idle_wait=0.02,
        observer_interval=0.02,
        clock=clock_a,
        poller=poll_recover_then_complete,
        as_json=True,
    )
    assert rc_a == 0
    res_a = json.loads(capsys.readouterr().out)['result']
    assert res_a['reason'] == 'event' and res_a['mode'] == 'owner'
    assert [e['kind'] for e in res_a['wake_events']] == ['terminal']
    assert calls_a[0] == 2

    # Subcase B: pre-seeded error_count=3, cluster still broken -> probe fails and exits promptly with error_count=4.
    ws_fail = tmp_path / 'still-broken'
    ws_fail.mkdir()
    m_b = JobMonitor(ws_fail)
    m_b.register('hpc', 'si-job', 'scf', '502', interval_seconds=900)
    _seed_task_fields(m_b, '502', error='RuntimeError', error_count=3)

    calls_b = [0]

    def poll_still_fail(_task):
        calls_b[0] += 1
        raise RuntimeError('still down')

    rc_b = watch_wait.run_wait(
        ws_fail,
        max_consecutive_errors=3,
        max_seconds=5.0,
        poller=poll_still_fail,
        as_json=True,
    )
    assert rc_b == 0
    res_b = json.loads(capsys.readouterr().out)['result']
    assert res_b['reason'] == 'error_threshold_reached' and res_b['mode'] == 'owner'
    assert calls_b[0] == 1
    assert res_b['error_jobs'][0]['error_count'] == 4


def test_watch_wait_reentry_falls_back_to_observer_when_lock_contended(tmp_path, capsys):
    m = JobMonitor(tmp_path)
    m.register('hpc', 'si-job', 'scf', '601', interval_seconds=900)
    _seed_task_fields(m, '601', error='RuntimeError', error_count=3)
    ev = event_outbox.append_event(
        tmp_path, _sample_event(job_id='601', kind='error', state='RUNNING', terminal=False, error='RuntimeError', error_count=1),
    )
    event_outbox.ack_events(tmp_path, [ev['event_id']])

    lock_path = agent_paths.monitor_paths(tmp_path)['worker_lock']
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    ext_lock = FileLock(lock_path, blocking=False)
    assert ext_lock.acquire()
    try:
        rc = watch_wait.run_wait(
            tmp_path,
            max_consecutive_errors=3,
            max_seconds=0.3,
            observer_interval=0.04,
            lock_probe_interval=0.05,
            poller=lambda _t: pytest.fail('must not call poller while external OS lock is held'),
            as_json=True,
        )
    finally:
        ext_lock.release()
    assert rc == 0
    res = json.loads(capsys.readouterr().out)['result']
    assert res['reason'] == 'timeout'
    assert res['mode'] == 'observer'
    assert res['daemon_detected'] is True


def test_watch_wait_observer_mode_and_automatic_failover_on_real_os_worker_lock(tmp_path, capsys):
    m = JobMonitor(tmp_path)
    m.register('hpc', 'si-job', 'scf', '701', interval_seconds=60)
    _seed_task_fields(m, '701', next_due_at=0.0)

    lock_path = agent_paths.monitor_paths(tmp_path)['worker_lock']
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    ext_lock = FileLock(lock_path, blocking=False)
    assert ext_lock.acquire()

    lock_released_at = [0.0]
    poll_times = []

    def release_later():
        time.sleep(0.25)
        lock_released_at[0] = time.monotonic()
        ext_lock.release()

    def poll(_task):
        poll_times.append(time.monotonic())
        return _poll_result(state='COMPLETED', terminal=True)

    releaser = threading.Thread(target=release_later, daemon=True)
    releaser.start()
    try:
        rc = watch_wait.run_wait(
            tmp_path,
            observer_interval=0.04,
            lock_probe_interval=0.05,
            max_idle_wait=0.04,
            max_seconds=5.0,
            poller=poll,
            as_json=True,
        )
    finally:
        releaser.join(timeout=3.0)
        ext_lock.release()

    assert rc == 0
    res = json.loads(capsys.readouterr().out)['result']
    assert res['reason'] == 'event'
    assert res['mode'] == 'owner'
    assert res['daemon_detected'] is True
    assert len(poll_times) == 1
    assert poll_times[0] >= lock_released_at[0]
    assert event_outbox.worker_lock_held(tmp_path) is False


def test_watch_wait_observer_unacked_vs_acked_error_and_timeout_exit_zero(tmp_path, capsys):
    m = JobMonitor(tmp_path)
    m.register('hpc', 'si-job', 'scf', '801', interval_seconds=900)
    _seed_task_fields(m, '801', error='RuntimeError', error_count=3)
    ev = event_outbox.append_event(
        tmp_path, _sample_event(job_id='801', kind='error', state='RUNNING', terminal=False, error='RuntimeError', error_count=1),
    )

    lock_path = agent_paths.monitor_paths(tmp_path)['worker_lock']
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    ext_lock = FileLock(lock_path, blocking=False)
    assert ext_lock.acquire()
    try:
        # 1. Unacked unrecovered error + error_count=3 -> wakes immediately in observer mode.
        rc1 = watch_wait.run_wait(
            tmp_path,
            max_consecutive_errors=3,
            max_seconds=2.0,
            observer_interval=0.04,
            poller=lambda _t: pytest.fail('must not poll under external lock'),
            as_json=True,
        )
        assert rc1 == 0
        res1 = json.loads(capsys.readouterr().out)['result']
        assert res1['reason'] == 'error_threshold_reached'
        assert res1['mode'] == 'observer'
        assert res1['daemon_detected'] is True

        # 2. Once acked, re-entering does NOT spin; external writer bumping error_count 3 -> 4 wakes it.
        event_outbox.ack_events(tmp_path, [ev['event_id']])

        def bump_error_later():
            time.sleep(0.15)
            _seed_task_fields(m, '801', error='RuntimeError', error_count=4)

        bumper = threading.Thread(target=bump_error_later, daemon=True)
        bumper.start()
        try:
            rc2 = watch_wait.run_wait(
                tmp_path,
                max_consecutive_errors=3,
                max_seconds=3.0,
                observer_interval=0.04,
                lock_probe_interval=0.05,
                poller=lambda _t: pytest.fail('must not poll under external lock'),
                as_json=True,
            )
        finally:
            bumper.join(timeout=3.0)
        assert rc2 == 0
        res2 = json.loads(capsys.readouterr().out)['result']
        assert res2['reason'] == 'error_threshold_reached'
        assert res2['mode'] == 'observer'
        assert res2['error_jobs'][0]['error_count'] == 4
    finally:
        ext_lock.release()


def test_watch_wait_poll_interval_accelerates_without_mutating_disk_or_breaking_backoff(tmp_path, capsys):
    clock = _scaled_clock(start=1000.0, scale=1000.0)
    m = JobMonitor(tmp_path, clock=clock)
    m.register('hpc', 'si-job', 'scf', '901', interval_seconds=600)

    poll_clocks = []

    def poll(_task):
        t = clock()
        poll_clocks.append(t)
        if len(poll_clocks) in (1, 2):
            raise RuntimeError('transient glitch')
        return _poll_result(state='COMPLETED', terminal=True)

    rc = watch_wait.run_wait(
        tmp_path,
        server='hpc',
        job_name='si-job',
        phase='scf',
        poll_interval=60,
        max_consecutive_errors=3,
        max_seconds=5.0,
        max_idle_wait=0.02,
        observer_interval=0.02,
        clock=clock,
        poller=poll,
        as_json=True,
    )
    assert rc == 0
    res = json.loads(capsys.readouterr().out)['result']
    assert res['reason'] == 'event' and res['mode'] == 'owner'
    assert len(poll_clocks) == 3
    # First poll happened well before the persisted 600s interval (tightened to 60s).
    assert poll_clocks[0] - 1000.0 < 450.0
    # After 2nd error (err_cnt=2), exponential backoff is 2 * 60 = 120s (strictly > 60s).
    assert poll_clocks[2] - poll_clocks[1] >= 110.0
    # Persisted job-monitor.json still retains original 600s interval_seconds.
    persisted = JobMonitor(tmp_path).status()['tasks'][0]
    assert persisted['interval_seconds'] == 600


def test_watch_wait_disabled_errored_job_does_not_spin_or_block_enabled_job(tmp_path, capsys):
    m = JobMonitor(tmp_path)
    m.register('hpc', 'job-disabled', 'scf', '910', interval_seconds=900)
    m.register('hpc', 'job-enabled', 'scf', '911', interval_seconds=60)
    _seed_task_fields(m, '910', enabled=False, error='RuntimeError', error_count=3)
    _seed_task_fields(m, '911', next_due_at=0.0)

    polled_ids = []

    def poll(task):
        polled_ids.append(task['job_id'])
        return _poll_result(state='COMPLETED', terminal=True)

    rc = watch_wait.run_wait(
        tmp_path,
        server='hpc',
        max_consecutive_errors=3,
        max_seconds=3.0,
        max_idle_wait=0.03,
        observer_interval=0.03,
        poller=poll,
        as_json=True,
    )
    assert rc == 0
    res = json.loads(capsys.readouterr().out)['result']
    assert res['reason'] == 'event' and res['mode'] == 'owner'
    assert polled_ids == ['911']
    assert [e['job_id'] for e in res['wake_events']] == ['911']
    assert res['error_jobs'] == []


def test_watch_wait_stop_event_and_cli_roundtrip_via_control_cli_main(tmp_path, capsys):
    # CLI roundtrip on clean workspace -> exit 0, autonomy_phase='P3_bounded_autonomy', reason='no_active_jobs'.
    rc = control_cli.main(['--workspace', str(tmp_path), 'watch', 'wait', '--json'])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload['ok'] is True
    assert payload['result']['autonomy_phase'] == 'P3_bounded_autonomy'
    assert payload['result']['reason'] == 'no_active_jobs'

    # External stop_event interrupts active wait cleanly with exit 0 and releases worker_lock.
    ws_stop = tmp_path / 'stop'
    ws_stop.mkdir()
    m = JobMonitor(ws_stop)
    m.register('hpc', 'si-job', 'scf', '950', interval_seconds=900)
    stop = threading.Event()
    threading.Timer(0.12, stop.set).start()
    rc_stop = watch_wait.run_wait(
        ws_stop,
        max_seconds=5.0,
        stop_event=stop,
        observer_interval=0.04,
        max_idle_wait=0.04,
        poller=lambda _t: _poll_result(),
        as_json=True,
    )
    assert rc_stop == 0
    res_stop = json.loads(capsys.readouterr().out)['result']
    assert res_stop['reason'] == 'interrupted'
    assert event_outbox.worker_lock_held(ws_stop) is False


def test_watch_wait_monitor_exited_returns_code_one_and_releases_lock(tmp_path, capsys):
    m = JobMonitor(tmp_path)
    m.register('hpc', 'si-job', 'scf', '960', interval_seconds=900)
    corrupted = [False]

    def corrupt_registry_later():
        time.sleep(0.08)
        m.path.write_text('{corrupt', encoding='utf-8', newline='\n')
        corrupted[0] = True

    breaker = threading.Thread(target=corrupt_registry_later, daemon=True)
    breaker.start()
    try:
        rc = watch_wait.run_wait(
            tmp_path,
            max_seconds=3.0,
            max_idle_wait=0.03,
            observer_interval=0.03,
            poller=lambda _t: _poll_result(),
            as_json=True,
        )
    finally:
        breaker.join(timeout=3.0)
    assert corrupted[0] is True
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload['ok'] is False
    assert event_outbox.worker_lock_held(tmp_path) is False

