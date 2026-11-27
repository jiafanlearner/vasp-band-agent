"""Offline TDD regression suite for P1c persistent event outbox, vasp_watch_jobs, and vasp-ctl watch."""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time

import pytest

from vasp_agent.common import agent_paths
from vasp_agent.control import MAX_RESPONSE_BYTES, ControlService
from vasp_agent.control import cli as control_cli
from vasp_agent.monitoring import event_outbox, job_monitor, polling as monitor_polling
from vasp_agent.monitoring.job_monitor import JobMonitor


def _poll_result(state: str = 'RUNNING', terminal: bool = False, **extra):
    return {
        'state': state,
        'terminal': terminal,
        'outcome': 'completed' if terminal else None,
        'workflow_status': 'awaiting_review' if terminal else 'running',
        'next_phase': 'relaxation' if terminal else 'convergence',
        **extra,
    }


def _now(minute: int = 0) -> datetime:
    return datetime(2026, 11, 26, 12, minute, 0, tzinfo=timezone.utc)


def _sample_event(**overrides):
    base = {
        'server': 'hpc',
        'job_name': 'si-auto-01',
        'phase': 'convergence',
        'job_id': '4242',
        'kind': 'terminal',
        'changed': True,
        'state': 'COMPLETED',
        'terminal': True,
        'outcome': 'completed',
        'workflow_status': 'awaiting_review',
        'next_phase': 'relaxation',
        'error': None,
        'error_count': 0,
        'message': '作业已结束，等待审阅。',
    }
    base.update(overrides)
    return base


# ----------------------------------------- 1. Outbox persistence, dedupe & ack


def test_outbox_read_only_calls_do_not_create_runtime_files(tmp_path):
    assert event_outbox.list_events(tmp_path) == []
    assert event_outbox.worker_lock_held(tmp_path) is False
    ack_res = event_outbox.ack_events(tmp_path, ['evt-20261126-aaaaaaaaaaaa'])
    assert ack_res == {
        'acked': [],
        'already_acked': [],
        'not_found': ['evt-20261126-aaaaaaaaaaaa'],
        'pending_count': 0,
    }
    assert not agent_paths.agent_root(tmp_path).exists()


def test_job_monitor_persists_only_changed_actionable_events_and_deduplicates(tmp_path):
    clock_val = [1000.0]
    m = JobMonitor(tmp_path, clock=lambda: clock_val[0])
    m.register('hpc', 'si-01', 'convergence', '1001')

    # Normal status transition (UNQUERIED -> RUNNING) must NOT pollute outbox.
    m._poller = lambda _t: _poll_result(state='RUNNING', terminal=False)
    clock_val[0] += 900
    assert m._check(False)[0]['kind'] == 'status'
    assert event_outbox.list_events(tmp_path) == []
    assert not agent_paths.file_path(tmp_path, 'monitor_events').exists()

    # Transient error (changed=True) IS persisted without leaking raw secret text.
    def fail(_t):
        raise OSError('secret_token=sk-never-write-me')

    m._poller = fail
    clock_val[0] += 900
    err_ev = m._check(False)[0]
    assert err_ev['kind'] == 'error' and err_ev['changed'] is True
    pending = event_outbox.list_events(tmp_path)
    assert len(pending) == 1
    assert pending[0]['kind'] == 'error'
    assert pending[0]['acked_at'] is None
    assert pending[0]['error'] == 'OSError'
    raw_bytes = agent_paths.file_path(tmp_path, 'monitor_events').read_bytes()
    assert b'\r\n' not in raw_bytes
    assert b'sk-never-write-me' not in raw_bytes

    # Repeated identical error (changed=False) does not duplicate outbox record.
    clock_val[0] += 900
    assert m._check(False)[0]['changed'] is False
    assert len(event_outbox.list_events(tmp_path)) == 1

    # Recovery (changed=True) appends recovered event.
    m._poller = lambda _t: _poll_result(state='RUNNING', terminal=False)
    clock_val[0] += 1800
    assert m._check(False)[0]['kind'] == 'recovered'
    pending = event_outbox.list_events(tmp_path)
    assert [e['kind'] for e in pending] == ['error', 'recovered']

    # Terminal completion discovered via manual check_once() even while paused.
    m.configure(enabled=False)
    m._poller = lambda _t: _poll_result(state='COMPLETED', terminal=True)
    manual = m.check_once()
    assert len(manual) == 1 and manual[0]['kind'] == 'terminal'
    pending = event_outbox.list_events(tmp_path)
    assert [e['kind'] for e in pending] == ['error', 'recovered', 'terminal']
    term = pending[-1]
    assert term['workflow_status'] == 'awaiting_review'
    assert term['next_phase'] == 'relaxation'
    assert term['server'] == 'hpc' and term['job_name'] == 'si-01' and term['job_id'] == '1001'
    assert term['dedupe_key'] == 'hpc|si-01|convergence|1001|terminal|COMPLETED'


def test_outbox_idempotent_dedupe_ack_and_history_retention(tmp_path):
    first = event_outbox.append_event(tmp_path, _sample_event(), now=_now(1))
    assert first is not None and first['acked_at'] is None

    # Duplicate terminal before ack returns existing record without appending.
    dup1 = event_outbox.append_event(tmp_path, _sample_event(), now=_now(2))
    assert dup1['event_id'] == first['event_id']
    assert len(event_outbox.list_events(tmp_path, include_acked=True)) == 1

    # Ack is idempotent: second ack preserves original acked_at timestamp.
    ack1 = event_outbox.ack_events(tmp_path, [first['event_id'], 'evt-20261126-missing00000'], acked_by='maker:a1', now=_now(3))
    assert ack1['acked'] == [first['event_id']]
    assert ack1['already_acked'] == []
    assert ack1['not_found'] == ['evt-20261126-missing00000']
    assert ack1['pending_count'] == 0

    ack2 = event_outbox.ack_events(tmp_path, [first['event_id']], acked_by='maker:a2', now=_now(4))
    assert ack2['acked'] == []
    assert ack2['already_acked'] == [first['event_id']]
    all_rows = event_outbox.list_events(tmp_path, include_acked=True)
    assert all_rows[0]['acked_at'] == '2026-11-26T12:03:00Z'
    assert all_rows[0]['acked_by'] == 'maker:a1'

    # Duplicate terminal AFTER ack is still suppressed.
    dup2 = event_outbox.append_event(tmp_path, _sample_event(), now=_now(5))
    assert dup2['event_id'] == first['event_id']
    assert len(event_outbox.list_events(tmp_path, include_acked=True)) == 1

    # Error -> ack -> recovered -> ack -> second error is NOT suppressed.
    err1 = event_outbox.append_event(
        tmp_path, _sample_event(job_id='77', kind='error', state='RUNNING', terminal=False, outcome=None, error='Timeout'),
        now=_now(6),
    )
    assert event_outbox.append_event(
        tmp_path, _sample_event(job_id='77', kind='error', state='RUNNING', terminal=False, outcome=None, error='Timeout'),
        now=_now(7),
    )['event_id'] == err1['event_id']
    event_outbox.ack_events(tmp_path, [err1['event_id']], now=_now(8))
    rec = event_outbox.append_event(
        tmp_path, _sample_event(job_id='77', kind='recovered', state='RUNNING', terminal=False, outcome=None),
        now=_now(9),
    )
    assert rec['event_id'] != err1['event_id']
    event_outbox.ack_events(tmp_path, [rec['event_id']], now=_now(10))
    err2 = event_outbox.append_event(
        tmp_path, _sample_event(job_id='77', kind='error', state='RUNNING', terminal=False, outcome=None, error='Timeout'),
        now=_now(11),
    )
    assert err2['event_id'] != err1['event_id']
    assert [e['event_id'] for e in event_outbox.list_events(tmp_path)] == [err2['event_id']]


def test_outbox_corrupt_jsonl_and_symlink_guards(tmp_path, monkeypatch):
    out_file = agent_paths.file_path(tmp_path, 'monitor_events')
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text('{bad json\n', encoding='utf-8', newline='\n')
    with pytest.raises(event_outbox.EventOutboxError) as exc:
        event_outbox.list_events(tmp_path)
    assert exc.value.error_code == 'OUTBOX_CORRUPTED'
    # Corrupt file is never clobbered on append or ack.
    with pytest.raises(event_outbox.EventOutboxError):
        event_outbox.append_event(tmp_path, _sample_event(), now=_now(1))
    assert out_file.read_text(encoding='utf-8') == '{bad json\n'

    # Mock symlink boundary test (runs on every OS).
    out_file.unlink()
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda p: True if p == out_file else real_is_symlink(p))
    with pytest.raises(event_outbox.EventOutboxError) as sym_exc:
        event_outbox.list_events(tmp_path)
    assert sym_exc.value.error_code == 'OUTBOX_SYMLINK_REFUSED'


def test_outbox_real_symlink_refused(tmp_path):
    target = tmp_path / 'outside.jsonl'
    target.write_text('', encoding='utf-8', newline='\n')
    out_file = agent_paths.file_path(tmp_path, 'monitor_events')
    out_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, out_file)
    except OSError:
        pytest.skip('OS does not permit creating symlinks without elevated privileges')
    with pytest.raises(event_outbox.EventOutboxError) as exc:
        event_outbox.append_event(tmp_path, _sample_event(), now=_now(1))
    assert exc.value.error_code == 'OUTBOX_SYMLINK_REFUSED'


# ---------------------------- 2. vasp_watch_jobs & worker_lock mutual exclusion


def test_vasp_watch_jobs_maker_poll_ack_and_checker_read_only_gate(tmp_path):
    checker = ControlService(tmp_path, role='checker', actor_id='chk-1')
    maker = ControlService(tmp_path, role='maker', actor_id='mk-1')

    # Checker read-only view on clean workspace creates nothing under .agent/state.
    empty = checker.call('vasp_watch_jobs', {})
    assert empty['ok'] is True
    assert empty['result']['pending_events'] == []
    assert empty['result']['active_jobs'] == []
    assert empty['result']['submission_enabled'] is False
    assert not agent_paths.file_path(tmp_path, 'monitor_events').exists()

    # Checker is refused when attempting poll=True or ack_ids.
    deny_poll = checker.call('vasp_watch_jobs', {'poll': True})
    assert deny_poll['ok'] is False
    assert deny_poll['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'

    deny_ack = checker.call('vasp_watch_jobs', {'ack_ids': ['evt-20261126-aaaaaaaaaaaa']})
    assert deny_ack['ok'] is False
    assert deny_ack['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'

    # Register two jobs; maker triggers poll=True.
    maker.box.monitor.register('hpc', 'si-a', 'convergence', '101', resources={'walltime': '00:30:00'})
    maker.box.monitor.register('hpc', 'si-b', 'scf', '102', resources={'walltime': '04:00:00'})

    def fake_poll(task):
        if task['job_id'] == '101':
            return _poll_result(state='COMPLETED', terminal=True)
        return _poll_result(state='RUNNING', terminal=False)

    maker.box.monitor._poller = fake_poll
    res = maker.call('vasp_watch_jobs', {'poll': True})
    assert res['ok'] is True
    body = res['result']
    assert body['polled'] is True and body['polled_count'] == 2
    assert body['daemon_running'] is False
    assert body['pending_count'] == 1
    assert len(body['pending_events']) == 1
    evt_id = body['pending_events'][0]['event_id']
    assert body['pending_events'][0]['job_name'] == 'si-a'
    assert [j['job_name'] for j in body['active_jobs']] == ['si-b']
    assert body['suggested_poll_seconds'] == 900
    assert len(json.dumps(res, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) <= MAX_RESPONSE_BYTES

    # Checker can read the pending event without mutating it.
    chk_view = checker.call('vasp_watch_jobs', {'job_name': 'si-a'})
    assert chk_view['ok'] is True
    assert chk_view['result']['pending_count'] == 1
    assert chk_view['result']['pending_events'][0]['event_id'] == evt_id

    # Maker acknowledges the event and can inspect acked history via include_acked=True.
    acked = maker.call('vasp_watch_jobs', {'ack_ids': [evt_id], 'include_acked': True})
    assert acked['ok'] is True
    assert acked['result']['ack_result']['acked'] == [evt_id]
    assert acked['result']['pending_count'] == 0
    assert acked['result']['pending_events'] == []
    assert [e['event_id'] for e in acked['result']['acked_events']] == [evt_id]


def test_vasp_watch_jobs_skips_ssh_poll_when_daemon_holds_worker_lock(tmp_path):
    clock_val = [1000.0]
    daemon = JobMonitor(tmp_path, clock=lambda: clock_val[0], max_idle_wait=0.05)
    daemon.register('hpc', 'si-daemon', 'scf', '555')
    polled_by_daemon = threading.Event()

    def daemon_poll(_task):
        polled_by_daemon.set()
        return _poll_result(state='COMPLETED', terminal=True, next_phase='bands')

    clock_val[0] += 900
    assert daemon.start(daemon_poll, lambda _e: None)
    try:
        assert polled_by_daemon.wait(timeout=3)
        # Wait briefly for transaction commit to finish writing outbox.
        deadline = time.time() + 3
        while time.time() < deadline and not event_outbox.list_events(tmp_path):
            time.sleep(0.02)

        assert event_outbox.worker_lock_held(tmp_path) is True
        maker = ControlService(tmp_path, role='maker', actor_id='mk-watch')
        maker.box.monitor._poller = lambda _t: pytest.fail('must skip SSH poll while daemon owns worker_lock')

        res = maker.call('vasp_watch_jobs', {'poll': True})
        assert res['ok'] is True
        body = res['result']
        assert body['daemon_running'] is True
        assert body['polled'] is False
        assert body['poll_skipped_reason'] == 'daemon_active'
        assert body['pending_count'] == 1
        assert body['pending_events'][0]['job_name'] == 'si-daemon'
        assert body['pending_events'][0]['next_phase'] == 'bands'
    finally:
        daemon.stop()
    assert event_outbox.worker_lock_held(tmp_path) is False


# ------------------------------------ 3. vasp-ctl watch once | daemon | ack


def test_vasp_ctl_watch_once_and_ack_cli(tmp_path, capsys, monkeypatch):
    from vasp_agent.monitoring import polling

    m = JobMonitor(tmp_path)
    m.register('hpc', 'si-cli', 'relaxation', '808')
    monkeypatch.setattr(polling, 'poll_job', lambda _ws, _task: _poll_result(state='COMPLETED', terminal=True, next_phase='scf'))

    rc = control_cli.main(['--workspace', str(tmp_path), 'watch', 'once', '--json'])
    assert rc == 0
    once_payload = json.loads(capsys.readouterr().out)
    assert once_payload['ok'] is True
    assert once_payload['result']['polled'] is True
    assert once_payload['result']['pending_count'] == 1
    evt_id = once_payload['result']['pending_events'][0]['event_id']

    # Human-readable watch once --no-poll prints table summary.
    rc = control_cli.main(['--workspace', str(tmp_path), 'watch', 'once', '--no-poll'])
    assert rc == 0
    human_out = capsys.readouterr().out
    assert 'si-cli' in human_out and evt_id in human_out

    # Invalid watch ack without ids or --all exits 2.
    rc = control_cli.main(['--workspace', str(tmp_path), 'watch', 'ack', '--json'])
    assert rc == 2
    err_payload = json.loads(capsys.readouterr().out)
    assert err_payload['ok'] is False and err_payload['error_code'] == 'INVALID_ARGUMENTS'

    # Ack specific event id.
    rc = control_cli.main(['--workspace', str(tmp_path), 'watch', 'ack', evt_id, '--json'])
    assert rc == 0
    ack_payload = json.loads(capsys.readouterr().out)
    assert ack_payload['ok'] is True
    assert ack_payload['result']['acked'] == [evt_id]
    assert ack_payload['result']['pending_count'] == 0

    # Add two more events and ack --all.
    e1 = event_outbox.append_event(tmp_path, _sample_event(job_id='901'), now=_now(12))
    e2 = event_outbox.append_event(tmp_path, _sample_event(job_id='902'), now=_now(13))
    rc = control_cli.main(['--workspace', str(tmp_path), 'watch', 'ack', '--all', '--json'])
    assert rc == 0
    ack_all = json.loads(capsys.readouterr().out)
    assert ack_all['ok'] is True
    assert set(ack_all['result']['acked']) == {e1['event_id'], e2['event_id']}
    assert ack_all['result']['pending_count'] == 0


def test_vasp_ctl_watch_daemon_runs_populates_outbox_and_refuses_second_instance(tmp_path, capsys, monkeypatch):
    from vasp_agent.monitoring import polling

    now = [1000.0]
    m = JobMonitor(tmp_path, clock=lambda: now[0])
    m.register('hpc', 'si-bg', 'bands', '909')
    # Make task immediately due by setting next_due_at in the past relative to real time.
    reg = json.loads(m.path.read_text(encoding='utf-8'))
    reg['tasks'][0]['next_due_at'] = 0.0
    m.path.write_text(json.dumps(reg, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')

    monkeypatch.setattr(polling, 'poll_job', lambda _ws, _task: _poll_result(state='COMPLETED', terminal=True, next_phase=None))

    # Bounded daemon run executes poll, persists outbox event, and releases worker_lock on exit.
    rc = control_cli.main([
        '--workspace', str(tmp_path), 'watch', 'daemon', '--max-idle-wait', '0.05', '--max-seconds', '0.3', '--json',
    ])
    assert rc == 0
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert any(row.get('event') == 'daemon_started' for row in lines)
    assert any(row.get('event') == 'daemon_stopped' for row in lines)
    pending = event_outbox.list_events(tmp_path)
    assert len(pending) == 1 and pending[0]['job_name'] == 'si-bg'
    assert event_outbox.worker_lock_held(tmp_path) is False

    # While another monitor owns worker_lock, watch daemon refuses with exit code 1.
    holder = JobMonitor(tmp_path, max_idle_wait=0.05)
    assert holder.start(lambda _t: _poll_result(), lambda _e: None)
    try:
        rc2 = control_cli.main([
            '--workspace', str(tmp_path), 'watch', 'daemon', '--max-idle-wait', '0.05', '--max-seconds', '0.2', '--json',
        ])
        assert rc2 == 1
        err = json.loads(capsys.readouterr().out)
        assert err['ok'] is False and err['error_code'] == 'WATCH_DAEMON_ALREADY_RUNNING'
    finally:
        holder.stop()


# ----------------------------- 4. Compaction, concurrency, legacy & CLI gates


def test_outbox_compacts_acked_history_without_dropping_pending(tmp_path, monkeypatch):
    monkeypatch.setattr(event_outbox, 'MAX_ACKED_HISTORY', 3)
    acked_ids = []
    for idx in range(5):
        ev = event_outbox.append_event(tmp_path, _sample_event(job_id=str(100 + idx)), now=_now(idx))
        acked_ids.append(ev['event_id'])
    p1 = event_outbox.append_event(tmp_path, _sample_event(job_id='201'), now=_now(10))
    p2 = event_outbox.append_event(tmp_path, _sample_event(job_id='202'), now=_now(11))

    res = event_outbox.ack_events(tmp_path, acked_ids, acked_by='maker:test', now=_now(12))
    assert res['acked'] == acked_ids
    assert res['pending_count'] == 2

    pending = event_outbox.list_events(tmp_path)
    assert [e['event_id'] for e in pending] == [p1['event_id'], p2['event_id']]
    acked = event_outbox.list_events(tmp_path, only_acked=True)
    assert [e['event_id'] for e in acked] == acked_ids[-3:]


def test_outbox_concurrent_appends_preserve_every_event(tmp_path):
    errors = []

    def worker(idx: int):
        try:
            event_outbox.append_event(tmp_path, _sample_event(job_id=str(500 + idx)), now=_now(idx % 59))
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert errors == []
    pending = event_outbox.list_events(tmp_path)
    assert len(pending) == 10
    assert {e['job_id'] for e in pending} == {str(500 + i) for i in range(10)}


def test_legacy_monitor_layout_shares_state_lock_with_event_outbox(tmp_path):
    legacy = tmp_path / '.job-monitor.json'
    legacy.write_text(
        json.dumps({'version': 1, 'enabled': True, 'auto': True, 'interval_seconds': 900, 'tasks': []}),
        encoding='utf-8',
        newline='\n',
    )
    m = JobMonitor(tmp_path)
    assert m.path == legacy
    m.register('hpc', 'si-legacy', 'scf', '701')
    m._poller = lambda _t: _poll_result(state='COMPLETED', terminal=True, next_phase='bands')
    m.check_once()
    pending = event_outbox.list_events(tmp_path)
    assert len(pending) == 1 and pending[0]['job_name'] == 'si-legacy'
    assert (tmp_path / '.job-monitor-state.lock').exists()
    assert agent_paths.file_path(tmp_path, 'monitor_events').is_file()


def test_vasp_ctl_watch_role_filters_and_corrupt_daemon_early_exit(tmp_path, capsys):
    # Checker may run watch once --no-poll, but is refused for poll, ack, and daemon.
    rc = control_cli.main(['--workspace', str(tmp_path), '--role', 'checker', 'watch', 'once', '--no-poll', '--json'])
    assert rc == 0
    assert json.loads(capsys.readouterr().out)['ok'] is True

    for argv in (
        ['--workspace', str(tmp_path), '--role', 'checker', 'watch', 'once', '--json'],
        ['--workspace', str(tmp_path), '--role', 'checker', 'watch', 'ack', '--all', '--json'],
        ['--workspace', str(tmp_path), '--role', 'checker', 'watch', 'daemon', '--max-seconds', '0.2', '--json'],
    ):
        rc = control_cli.main(argv)
        assert rc == 1
        err = json.loads(capsys.readouterr().out)
        assert err['ok'] is False and err['error_code'] == 'TOOL_NOT_ALLOWED_FOR_ROLE'

    # Invalid server filter exits 2.
    rc = control_cli.main(['--workspace', str(tmp_path), 'watch', 'once', '--no-poll', '--server', 'bad;srv', '--json'])
    assert rc == 2
    assert json.loads(capsys.readouterr().out)['error_code'] == 'INVALID_ARGUMENTS'

    # Corrupt registry causes daemon to exit promptly with code 1 instead of waiting max-seconds.
    reg = agent_paths.monitor_paths(tmp_path)['registry']
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text('{corrupt', encoding='utf-8', newline='\n')
    started = time.monotonic()
    rc = control_cli.main([
        '--workspace', str(tmp_path), 'watch', 'daemon', '--max-idle-wait', '0.05', '--max-seconds', '5.0', '--json',
    ])
    elapsed = time.monotonic() - started
    assert rc == 1 and elapsed < 2.5
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[0]['event'] == 'daemon_started'
    assert lines[-1]['event'] == 'daemon_stopped' and lines[-1]['reason'] == 'monitor_exited'
    assert event_outbox.worker_lock_held(tmp_path) is False


# ------------------------------------ 5. P1c review regression tests


def test_corrupt_or_symlink_outbox_does_not_deactivate_terminal_job(tmp_path, monkeypatch):
    m = JobMonitor(tmp_path)
    m.register('hpc', 'si-keep', 'convergence', '3001')
    m._poller = lambda _t: _poll_result(state='COMPLETED', terminal=True, next_phase='relaxation')

    # Corrupt outbox: check_once fails BEFORE deactivating job-monitor.json entry.
    out_file = agent_paths.file_path(tmp_path, 'monitor_events')
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text('{corrupt\n', encoding='utf-8', newline='\n')
    with pytest.raises(event_outbox.EventOutboxError) as exc:
        m.check_once()
    assert exc.value.error_code == 'OUTBOX_CORRUPTED'
    task_row = m.status()['tasks'][0]
    assert task_row['active'] is True and task_row['terminal'] is False

    # Symlink outbox (mocked): check_once fails BEFORE deactivating job-monitor.json entry.
    out_file.unlink()
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, 'is_symlink', lambda p: True if p == out_file else real_is_symlink(p))
    with pytest.raises(event_outbox.EventOutboxError) as sym_exc:
        m.check_once()
    assert sym_exc.value.error_code == 'OUTBOX_SYMLINK_REFUSED'
    assert m.status()['tasks'][0]['active'] is True

    # Once outbox is healthy again, next poll persists terminal event and deactivates task.
    monkeypatch.setattr(Path, 'is_symlink', real_is_symlink)
    checked = m.check_once()
    assert len(checked) == 1 and checked[0]['kind'] == 'terminal'
    assert m.status()['tasks'][0]['active'] is False
    pending = event_outbox.list_events(tmp_path)
    assert len(pending) == 1 and pending[0]['job_name'] == 'si-keep'


def test_unacked_error_recovered_error_preserves_all_three_transitions(tmp_path):
    e1 = event_outbox.append_event(
        tmp_path,
        _sample_event(job_id='88', kind='error', state='RUNNING', terminal=False, outcome=None, error='Timeout'),
        now=_now(1),
    )
    e2 = event_outbox.append_event(
        tmp_path,
        _sample_event(job_id='88', kind='recovered', state='RUNNING', terminal=False, outcome=None),
        now=_now(2),
    )
    e3 = event_outbox.append_event(
        tmp_path,
        _sample_event(job_id='88', kind='error', state='RUNNING', terminal=False, outcome=None, error='Timeout'),
        now=_now(3),
    )
    # All three events remain unacked in order; e3 must NOT be swallowed by unacked e1.
    pending = event_outbox.list_events(tmp_path)
    assert [row['event_id'] for row in pending] == [e1['event_id'], e2['event_id'], e3['event_id']]
    assert [row['kind'] for row in pending] == ['error', 'recovered', 'error']
    # Consecutive identical error right after e3 is still deduplicated.
    e3_dup = event_outbox.append_event(
        tmp_path,
        _sample_event(job_id='88', kind='error', state='RUNNING', terminal=False, outcome=None, error='Timeout'),
        now=_now(4),
    )
    assert e3_dup['event_id'] == e3['event_id']
    assert len(event_outbox.list_events(tmp_path)) == 3


def test_watch_daemon_human_monitor_error_nan_inf_and_startup_cleanup(tmp_path, capsys, monkeypatch):
    # --max-seconds nan and inf must exit 2 (INVALID_ARGUMENTS).
    for bad in ('nan', 'inf', '-inf'):
        rc = control_cli.main([
            '--workspace', str(tmp_path), 'watch', 'daemon', f'--max-seconds={bad}', '--json',
        ])
        assert rc == 2
        err = json.loads(capsys.readouterr().out)
        assert err['ok'] is False and err['error_code'] == 'INVALID_ARGUMENTS'

    # Non-JSON mode prints monitor-level error message (not None/None JobID=None).
    reg = agent_paths.monitor_paths(tmp_path)['registry']
    reg.parent.mkdir(parents=True, exist_ok=True)
    reg.write_text('{corrupt', encoding='utf-8', newline='\n')
    rc = control_cli.main([
        '--workspace', str(tmp_path), 'watch', 'daemon', '--max-idle-wait', '0.05', '--max-seconds', '2.0',
    ])
    assert rc == 1
    out = capsys.readouterr().out
    assert '监控记录无法读取或保存' in out
    assert 'None/None' not in out
    assert event_outbox.worker_lock_held(tmp_path) is False

    # Startup write failure (e.g. BrokenPipeError) still stops monitor and releases worker_lock.
    reg.unlink()
    from vasp_agent.control import watch_cli

    class _BrokenStdout:
        def write(self, _s):
            raise BrokenPipeError('pipe closed')

        def flush(self):
            pass

    monkeypatch.setattr(watch_cli.sys, 'stdout', _BrokenStdout())
    with pytest.raises(BrokenPipeError):
        watch_cli.run_daemon(tmp_path, max_idle_wait=0.05, max_seconds=1.0, as_json=False)
    assert event_outbox.worker_lock_held(tmp_path) is False


def test_format_utc_naive_datetime_and_invalid_error_count(tmp_path):
    naive = datetime(2026, 11, 26, 8, 30, 0)
    assert event_outbox._format_utc(naive) == '2026-11-26T08:30:00Z'
    rec = event_outbox.append_event(tmp_path, _sample_event(job_id='999'), now=naive)
    assert rec['occurred_at'] == '2026-11-26T08:30:00Z'
    assert rec['event_id'].startswith('evt-20261126-')

    for bad_count in ('oops', -1, True):
        with pytest.raises(event_outbox.EventOutboxError) as exc:
            event_outbox.append_event(tmp_path, _sample_event(job_id='998', error_count=bad_count), now=naive)
        assert exc.value.error_code == 'OUTBOX_INVALID_EVENT'


# ------------------------------------ 6. Layer 3 AST zero-upward-import guard


def test_layer3_monitoring_modules_have_zero_upward_imports():
    """Layer 3 monitoring modules must never import Layer 4 chat/ or control/."""
    forbidden = ('chat', 'control')
    violations = []
    for mod in (event_outbox, job_monitor, monitor_polling):
        mod_path = Path(mod.__file__)
        tree = ast.parse(mod_path.read_text(encoding='utf-8'))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for pkg in forbidden:
                        if alias.name == f'vasp_agent.{pkg}' or alias.name.startswith(f'vasp_agent.{pkg}.'):
                            violations.append(f'{mod_path.name}:{node.lineno} -> {alias.name}')
            elif isinstance(node, ast.ImportFrom):
                mod_name = node.module or ''
                for pkg in forbidden:
                    if mod_name == f'vasp_agent.{pkg}' or mod_name.startswith(f'vasp_agent.{pkg}.'):
                        violations.append(f'{mod_path.name}:{node.lineno} -> {mod_name}')
                    elif node.level >= 2 and (mod_name == pkg or mod_name.startswith(f'{pkg}.')):
                        violations.append(f'{mod_path.name}:{node.lineno} -> {"." * node.level}{mod_name}')
                    elif node.level >= 2 and not mod_name:
                        for alias in node.names:
                            if alias.name == pkg:
                                violations.append(f'{mod_path.name}:{node.lineno} -> {"." * node.level}{alias.name}')
    assert violations == []
