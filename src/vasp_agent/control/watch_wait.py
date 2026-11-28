"""Event-driven zero-idle blocking wait (`vasp-ctl watch wait`) for Layer 4 control plane."""
from __future__ import annotations

import json
import math
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any, Callable

from vasp_agent.control import service_contracts, service_watch
from vasp_agent.monitoring import event_outbox, polling
from vasp_agent.monitoring.job_monitor import JobMonitor, MonitorPersistenceError
from vasp_agent.workflow import stage_definitions
import vasp_agent.workflow.vasp_band_agent as core

JobKey = tuple[str, str, str | None, str]

_REASON_LABELS = {
    'event': '收到待处理监控事件',
    'error_threshold_reached': '匹配作业连续轮询失败达到阈值',
    'no_active_jobs': '当前无匹配活跃作业',
    'monitor_disabled': '监控已暂停（请先执行 /watch on 开启监控）',
    'timeout': '已达到最大等待时长（--max-seconds）',
    'interrupted': '等待被中断信号停止',
    'monitor_exited': '后台监控线程异常退出',
}


def _emit_json(payload: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + '\n')
    sys.stdout.flush()


def _emit_error(exc: Exception, *, as_json: bool, default_code: str = 'WATCH_ERROR') -> int:
    code = getattr(exc, 'error_code', default_code)
    if as_json:
        _emit_json({
            'ok': False,
            'error_code': code,
            'error': str(exc),
            'type': type(exc).__name__,
            'recoverable': False,
        })
    else:
        sys.stderr.write(f'[{code}] {exc}\n')
    return 2 if code == 'INVALID_ARGUMENTS' else 1


def _job_key(item: dict[str, Any]) -> JobKey:
    return (
        str(item.get('server') or ''),
        str(item.get('job_name') or ''),
        item.get('phase'),
        str(item.get('job_id') or ''),
    )


def _matches_filters(task: dict[str, Any], *, server: str | None, job_name: str | None, phase: str | None) -> bool:
    return (
        (server is None or task.get('server') == server)
        and (job_name is None or task.get('job_name') == job_name)
        and (phase is None or task.get('phase') == phase)
    )


def _check_positive_finite(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise core.ToolError('INVALID_ARGUMENTS', f'{name} 必须为大于 0 的有限数值。')


def _validate_args(
    *,
    server: str | None,
    job_name: str | None,
    phase: str | None,
    poll_interval: int | None,
    max_consecutive_errors: int,
    wake_on_error: bool,
    max_seconds: float,
    observer_interval: float,
    lock_probe_interval: float,
    max_idle_wait: float,
) -> None:
    if server is not None and (not isinstance(server, str) or not service_watch._SERVER_RE.fullmatch(server)):
        raise core.ToolError('INVALID_ARGUMENTS', f'无效 server 过滤条件：{server!r}。')
    if job_name is not None and (not isinstance(job_name, str) or not service_watch._JOB_RE.fullmatch(job_name)):
        raise core.ToolError('INVALID_ARGUMENTS', f'无效 job_name 过滤条件：{job_name!r}。')
    if phase is not None and (not isinstance(phase, str) or phase not in stage_definitions.known_names()):
        raise core.ToolError('INVALID_ARGUMENTS', f'无效 phase 过滤条件：{phase!r}。')
    if poll_interval is not None and (type(poll_interval) is not int or not 60 <= poll_interval <= 600):
        raise core.ToolError('INVALID_ARGUMENTS', 'poll_interval 必须为 60–600 秒的整数。')
    if type(max_consecutive_errors) is not int or not 1 <= max_consecutive_errors <= 50:
        raise core.ToolError('INVALID_ARGUMENTS', 'max_consecutive_errors 必须为 1–50 的整数。')
    if type(wake_on_error) is not bool:
        raise core.ToolError('INVALID_ARGUMENTS', 'wake_on_error 必须为布尔值。')
    for val, name in (
        (max_seconds, 'max_seconds'),
        (observer_interval, 'observer_interval'),
        (lock_probe_interval, 'lock_probe_interval'),
        (max_idle_wait, 'max_idle_wait'),
    ):
        _check_positive_finite(val, name)


def _active_unrecovered_errors(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return pending error events not superseded by a later recovered/terminal event for the exact same job."""
    by_key: dict[JobKey, list[str]] = {}
    for ev in events:
        kind, key = ev.get('kind'), _job_key(ev)
        if kind == 'error':
            by_key.setdefault(key, []).append(ev['event_id'])
        elif kind in ('recovered', 'terminal'):
            by_key.pop(key, None)
    surviving = {eid for ids in by_key.values() for eid in ids}
    return [ev for ev in events if ev.get('kind') == 'error' and ev.get('event_id') in surviving]


def _evaluate_state(
    pending_events: list[dict[str, Any]],
    active_jobs: list[dict[str, Any]],
    monitor_enabled: bool,
    *,
    wake_on_error: bool,
    max_consecutive_errors: int,
    baseline_errors: dict[JobKey, int] | None = None,
    check_error_threshold: bool = True,
) -> tuple[str | None, list[dict[str, Any]], list[dict[str, Any]]]:
    unrecovered_ids = {ev['event_id'] for ev in _active_unrecovered_errors(pending_events)}
    wake_events: list[dict[str, Any]] = []
    for ev in pending_events:
        kind = ev.get('kind')
        if kind == 'terminal':
            wake_events.append(ev)
        elif kind == 'error' and ev.get('event_id') in unrecovered_ids:
            if wake_on_error or int(ev.get('error_count') or 0) >= max_consecutive_errors:
                wake_events.append(ev)

    error_jobs = [
        job for job in active_jobs
        if job.get('enabled', True) and int(job.get('error_count') or 0) >= max_consecutive_errors
    ]
    if wake_events:
        return 'event', wake_events, error_jobs
    if check_error_threshold:
        triggered = [
            job for job in error_jobs
            if baseline_errors is None
            or _job_key(job) not in baseline_errors
            or int(job.get('error_count') or 0) > baseline_errors[_job_key(job)]
        ]
        if triggered:
            return 'error_threshold_reached', [], triggered
    if not active_jobs:
        return 'no_active_jobs', [], []
    if not monitor_enabled or all(job.get('enabled') is False for job in active_jobs):
        return 'monitor_disabled', [], error_jobs
    return None, [], error_jobs


def _read_snapshot(
    root: Path, monitor: JobMonitor, *, server: str | None, job_name: str | None, phase: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    pending = event_outbox.list_events(root, include_acked=False, server=server, job_name=job_name, phase=phase)
    active, enabled = service_watch._read_active_jobs(monitor, server=server, job_name=job_name, phase=phase)
    return pending, active, enabled


def _prune_baseline(baseline: dict[JobKey, int], active_jobs: list[dict[str, Any]], threshold: int) -> None:
    counts = {_job_key(job): int(job.get('error_count') or 0) for job in active_jobs}
    for key in list(baseline):
        if counts.get(key, 0) < threshold:
            baseline.pop(key, None)


def _render_human_summary(result: dict[str, Any]) -> str:
    reason = result['reason']
    lines = [
        f"监控等待结束：{_REASON_LABELS.get(reason, reason)}（reason={reason}，mode={result['mode']}，耗时 {result['waited_seconds']}s）",
        f"  待确认事件：{result['pending_count']} 条 · 唤醒事件：{len(result['wake_events'])} 条 · 活跃作业：{len(result['active_jobs'])} 个",
    ]
    for ev in result['wake_events']:
        lines.append(
            f"  [wake:{ev.get('kind')}] {ev.get('server')}/{ev.get('job_name')} "
            f"({ev.get('phase') or '-'}) JobID={ev.get('job_id')} state={ev.get('state')} id={ev.get('event_id')}"
        )
    for job in result['error_jobs']:
        lines.append(
            f"  [error_job] {job.get('server')}/{job.get('job_name')} "
            f"({job.get('phase') or '-'}) JobID={job.get('job_id')} error_count={job.get('error_count')} error={job.get('error')}"
        )
    return '\n'.join(lines)


def run_wait(
    workspace: Path,
    *,
    server: str | None = None,
    job_name: str | None = None,
    phase: str | None = None,
    poll_interval: int | None = None,
    max_consecutive_errors: int = 3,
    wake_on_error: bool = False,
    max_seconds: float = 14400.0,
    as_json: bool = False,
    poller: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    stop_event: threading.Event | None = None,
    clock: Callable[[], float] | None = None,
    observer_interval: float = 1.0,
    lock_probe_interval: float = 2.0,
    max_idle_wait: float = 30.0,
) -> int:
    """Silently block until a matching terminal event or consecutive-error threshold wakes the caller."""
    try:
        _validate_args(
            server=server, job_name=job_name, phase=phase, poll_interval=poll_interval,
            max_consecutive_errors=max_consecutive_errors, wake_on_error=wake_on_error,
            max_seconds=max_seconds, observer_interval=observer_interval,
            lock_probe_interval=lock_probe_interval, max_idle_wait=max_idle_wait,
        )
    except core.ToolError as exc:
        return _emit_error(exc, as_json=as_json)

    started_at = time.monotonic()
    filters = {'server': server, 'job_name': job_name, 'phase': phase}

    def _finish(
        reason: str, mode: str, daemon_detected: bool,
        wake_events: list[dict[str, Any]], pending_events: list[dict[str, Any]],
        active_jobs: list[dict[str, Any]], error_jobs: list[dict[str, Any]],
    ) -> int:
        result = {
            'autonomy_phase': service_contracts.AUTONOMY_PHASE,
            'reason': reason,
            'mode': mode,
            'daemon_detected': daemon_detected,
            'waited_seconds': round(max(0.0, time.monotonic() - started_at), 3),
            'filters': filters,
            'wake_events': wake_events,
            'pending_count': len(pending_events),
            'pending_events': pending_events,
            'active_jobs': active_jobs,
            'error_jobs': error_jobs,
        }
        ok_exit = reason != 'monitor_exited'
        if as_json:
            payload: dict[str, Any] = {'ok': ok_exit, 'result': result}
            if not ok_exit:
                payload['error_code'] = 'MONITOR_EXITED'
                payload['error'] = '后台监控线程异常退出，请检查监控记录与事件队列。'
            _emit_json(payload)
        else:
            sys.stdout.write(_render_human_summary(result) + '\n')
            sys.stdout.flush()
        return 0 if ok_exit else 1

    has_filters = any(v is not None for v in (server, job_name, phase))
    poll_filter = (
        (lambda task: _matches_filters(task, server=server, job_name=job_name, phase=phase))
        if has_filters else None
    )

    try:
        root = Path(workspace).resolve(strict=True)
        monitor = JobMonitor(
            root, clock=clock, max_idle_wait=max_idle_wait,
            poll_interval_override=poll_interval, poll_filter=poll_filter,
        )
        poll_fn = poller or (lambda task: polling.poll_job(root, task))
        monitor._poller = poll_fn

        pending, active, enabled = _read_snapshot(root, monitor, server=server, job_name=job_name, phase=phase)
        reason, wake_events, error_jobs = _evaluate_state(
            pending, active, enabled, wake_on_error=wake_on_error,
            max_consecutive_errors=max_consecutive_errors, check_error_threshold=False,
        )
        if reason is not None:
            return _finish(reason, 'immediate', False, wake_events, pending, active, error_jobs)

        daemon_detected = False
        baseline_errors: dict[JobKey, int] = {}
        errored_keys = {
            _job_key(job) for job in active
            if job.get('enabled', True) and int(job.get('error_count') or 0) >= max_consecutive_errors
        }
        if errored_keys:
            monitor.check_once(task_filter=lambda task: _job_key(task) in errored_keys)
            pending, active, enabled = _read_snapshot(root, monitor, server=server, job_name=job_name, phase=phase)
            if monitor._last_check_skipped_locked:
                daemon_detected = True
                reason, wake_events, error_jobs = _evaluate_state(
                    pending, active, enabled, wake_on_error=wake_on_error,
                    max_consecutive_errors=max_consecutive_errors, check_error_threshold=False,
                )
                if reason is not None:
                    return _finish(reason, 'observer', True, wake_events, pending, active, error_jobs)
                fresh_errored = [
                    job for job in active
                    if job.get('enabled', True) and int(job.get('error_count') or 0) >= max_consecutive_errors
                ]
                unrecovered_keys = {_job_key(ev) for ev in _active_unrecovered_errors(pending)}
                unacked_errored = [job for job in fresh_errored if _job_key(job) in unrecovered_keys]
                if unacked_errored:
                    return _finish('error_threshold_reached', 'observer', True, [], pending, active, unacked_errored)
                for job in fresh_errored:
                    baseline_errors[_job_key(job)] = int(job.get('error_count') or 0)
            else:
                reason, wake_events, error_jobs = _evaluate_state(
                    pending, active, enabled, wake_on_error=wake_on_error,
                    max_consecutive_errors=max_consecutive_errors, check_error_threshold=True,
                )
                if reason is not None:
                    return _finish(reason, 'owner', False, wake_events, pending, active, error_jobs)

        stop = stop_event or threading.Event()
        wake_signal = threading.Event()
        mode = 'observer'
        prev_sigterm, installed_sigterm = None, False
        deadline = started_at + max_seconds
        next_lock_probe = time.monotonic() + lock_probe_interval
        reason, wake_events, error_jobs = 'timeout', [], []
        try:
            if threading.current_thread() is threading.main_thread() and hasattr(signal, 'SIGTERM'):
                def _on_sigterm(*_args: Any) -> None:
                    stop.set()
                    wake_signal.set()
                try:
                    prev_sigterm = signal.getsignal(signal.SIGTERM)
                    signal.signal(signal.SIGTERM, _on_sigterm)
                    installed_sigterm = True
                except (OSError, ValueError):
                    installed_sigterm = False

            if monitor.start(poll_fn, lambda _ev: wake_signal.set()):
                mode = 'owner'
            else:
                daemon_detected = True

            while True:
                if stop.is_set():
                    reason = 'interrupted'
                    break
                if mode == 'owner' and (monitor._thread is None or not monitor._thread.is_alive()):
                    reason = 'monitor_exited'
                    break
                wake_signal.clear()
                pending, active, enabled = _read_snapshot(root, monitor, server=server, job_name=job_name, phase=phase)
                _prune_baseline(baseline_errors, active, max_consecutive_errors)
                reason, wake_events, error_jobs = _evaluate_state(
                    pending, active, enabled, wake_on_error=wake_on_error,
                    max_consecutive_errors=max_consecutive_errors,
                    baseline_errors=baseline_errors, check_error_threshold=True,
                )
                if reason is not None:
                    break
                now_mono = time.monotonic()
                remaining = deadline - now_mono
                if remaining <= 0:
                    reason = 'timeout'
                    break
                if mode == 'observer' and now_mono >= next_lock_probe:
                    next_lock_probe = now_mono + lock_probe_interval
                    if monitor.start(poll_fn, lambda _ev: wake_signal.set()):
                        mode = 'owner'
                        continue
                step_limit = min(observer_interval, max_idle_wait) if mode == 'owner' else observer_interval
                if mode == 'observer':
                    step_limit = min(step_limit, max(0.005, next_lock_probe - now_mono))
                wait_for = min(step_limit, remaining, 0.25 if stop_event is not None else step_limit)
                wake_signal.wait(timeout=max(0.005, wait_for))
        except KeyboardInterrupt:
            reason = 'interrupted'
        finally:
            try:
                monitor.stop()
            finally:
                if installed_sigterm:
                    signal.signal(signal.SIGTERM, prev_sigterm)

        return _finish(reason, mode, daemon_detected, wake_events, pending, active, error_jobs)
    except (core.ToolError, event_outbox.EventOutboxError, MonitorPersistenceError, OSError, ValueError) as exc:
        return _emit_error(exc, as_json=as_json)
