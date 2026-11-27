"""CLI subcommand handlers for `vasp-ctl watch once|daemon|ack`."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any, Callable

from vasp_agent.control import service_watch
from vasp_agent.monitoring import event_outbox, polling
from vasp_agent.monitoring.job_monitor import JobMonitor, MonitorPersistenceError
from vasp_agent.workflow import stage_definitions
import vasp_agent.workflow.vasp_band_agent as core


def register_watch_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Attach `vasp-ctl watch {once,daemon,ack}` subcommands."""
    watch_cmd = subparsers.add_parser(
        'watch',
        help='Poll active scheduler jobs, inspect the persistent event outbox, or run the watch daemon.',
    )
    sub = watch_cmd.add_subparsers(dest='watch_action', required=True)

    once = sub.add_parser('once', help='Poll due/active jobs once (unless daemon holds worker_lock) and print outbox.')
    once.add_argument('--no-poll', action='store_true', help='Skip scheduler polling and read cached outbox/jobs only.')
    once.add_argument('--include-acked', action='store_true', help='Include already-acknowledged events.')
    once.add_argument('--server', default=None, help='Optional server filter (e.g. local or server name).')
    once.add_argument('--job-name', default=None, help='Optional job_name filter.')
    once.add_argument('--phase', choices=stage_definitions.known_names(), default=None, help='Optional phase filter.')
    once.add_argument('--limit', type=int, default=50, help='Maximum events to return (1-200, default: 50).')
    once.add_argument('--json', action='store_true', dest='as_json', help='Emit machine-readable JSON on stdout.')

    daemon = sub.add_parser('daemon', help='Run foreground read-only JobMonitor loop populating monitor-events.jsonl.')
    daemon.add_argument(
        '--max-idle-wait',
        type=float,
        default=30.0,
        help='Maximum idle sleep between checks in seconds (0.05-300, default: 30.0).',
    )
    daemon.add_argument(
        '--max-seconds',
        type=float,
        default=None,
        help='Optional bounded runtime in seconds before clean shutdown (default: run until interrupted).',
    )
    daemon.add_argument('--json', action='store_true', dest='as_json', help='Emit NDJSON lifecycle/event lines.')

    ack = sub.add_parser('ack', help='Idempotently acknowledge persisted monitor events.')
    ack.add_argument('event_ids', nargs='*', help='One or more event IDs (evt-...) to acknowledge.')
    ack.add_argument('--all', action='store_true', dest='ack_all', help='Acknowledge all currently pending events.')
    ack.add_argument('--json', action='store_true', dest='as_json', help='Emit machine-readable JSON on stdout.')


def _emit_json(payload: dict[str, Any], *, compact: bool = False) -> None:
    if compact:
        sys.stdout.write(json.dumps(payload, ensure_ascii=False, separators=(',', ':'), default=str) + '\n')
    else:
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
    return 1


def _handle_once(workspace: Path, args: argparse.Namespace) -> int:
    if args.limit < 1 or args.limit > 200:
        _emit_error(
            core.ToolError('INVALID_ARGUMENTS', 'limit 必须为 1–200 的整数。'),
            as_json=args.as_json,
        )
        return 2
    result = service_watch.watch_jobs(
        workspace,
        role=args.role,
        actor_id=args.actor_id or 'vasp-ctl',
        poll=not bool(args.no_poll),
        include_acked=bool(args.include_acked),
        server=args.server,
        job_name=args.job_name,
        phase=args.phase,
        limit=args.limit,
    )
    if args.as_json:
        _emit_json({'ok': True, 'result': result})
    else:
        sys.stdout.write(service_watch.render_watch_summary(result) + '\n')
    return 0


def _handle_ack(workspace: Path, args: argparse.Namespace) -> int:
    if args.role != 'maker':
        return _emit_error(
            core.ToolError('TOOL_NOT_ALLOWED_FOR_ROLE', '只读审查角色（checker）不能确认（ack）监控事件。'),
            as_json=args.as_json,
        )
    if bool(args.ack_all) == bool(args.event_ids):
        _emit_error(
            core.ToolError('INVALID_ARGUMENTS', '请提供至少一个 event_id，或单独使用 --all。'),
            as_json=args.as_json,
        )
        return 2
    result = event_outbox.ack_events(
        workspace,
        args.event_ids if not args.ack_all else None,
        ack_all=bool(args.ack_all),
        acked_by=f'cli:{args.actor_id or "vasp-ctl"}',
    )
    if args.as_json:
        _emit_json({'ok': True, 'result': result})
    else:
        sys.stdout.write(
            f"已确认 {len(result['acked'])} 条事件（重复确认 {len(result['already_acked'])} 条，"
            f"未找到 {len(result['not_found'])} 条；剩余待确认 {result['pending_count']} 条）。\n"
        )
    return 0


def run_daemon(
    workspace: Path,
    *,
    max_idle_wait: float = 30.0,
    max_seconds: float | None = None,
    as_json: bool = False,
    poller: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    stop_event: threading.Event | None = None,
) -> int:
    """Run JobMonitor in the foreground, holding worker_lock and populating monitor-events.jsonl."""
    if not math.isfinite(max_idle_wait) or not (0.05 <= max_idle_wait <= 300.0):
        _emit_error(core.ToolError('INVALID_ARGUMENTS', 'max_idle_wait 必须在 0.05–300 秒之间。'), as_json=as_json)
        return 2
    if max_seconds is not None and (not math.isfinite(max_seconds) or max_seconds <= 0):
        _emit_error(core.ToolError('INVALID_ARGUMENTS', 'max_seconds 必须为大于 0 的有限数值。'), as_json=as_json)
        return 2

    stop = stop_event or threading.Event()
    monitor = JobMonitor(workspace, max_idle_wait=max_idle_wait)
    poll_fn = poller or (lambda task: polling.poll_job(workspace, task))
    stdout_lock = threading.Lock()
    started = threading.Event()

    def _emit_line(payload: dict[str, Any], text: str) -> None:
        with stdout_lock:
            if as_json:
                _emit_json(payload, compact=True)
            else:
                sys.stdout.write(text + '\n')
                sys.stdout.flush()

    def _notify(event: dict[str, Any]) -> None:
        started.wait(timeout=2.0)
        if event.get('job_id') is None:
            text = f"[{event.get('kind') or 'info'}] {event.get('message') or ''}".rstrip()
        else:
            text = (
                f"[{event.get('kind')}] {event.get('server')}/{event.get('job_name')} "
                f"({event.get('phase') or '-'}) JobID={event.get('job_id')} state={event.get('state')}"
            )
        _emit_line({'event': 'monitor_event', 'data': event}, text)

    if not monitor.start(poll_fn, _notify):
        return _emit_error(
            core.ToolError('WATCH_DAEMON_ALREADY_RUNNING', '已有监控守护进程或交互会话持有 worker_lock，未重复启动。'),
            as_json=as_json,
        )

    prev_sigterm = None
    installed_sigterm = False
    reason = 'stopped'
    try:
        if threading.current_thread() is threading.main_thread() and hasattr(signal, 'SIGTERM'):
            try:
                prev_sigterm = signal.getsignal(signal.SIGTERM)
                signal.signal(signal.SIGTERM, lambda *_a: stop.set())
                installed_sigterm = True
            except (OSError, ValueError):
                installed_sigterm = False

        _emit_line(
            {'ok': True, 'event': 'daemon_started', 'workspace': workspace.name, 'max_idle_wait': max_idle_wait},
            f'监控守护进程已启动（workspace={workspace.name}，max_idle_wait={max_idle_wait}s）。',
        )
        started.set()

        deadline = (time.monotonic() + max_seconds) if max_seconds is not None else None
        while True:
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    reason = 'max_seconds_reached'
                    break
                wait_for = min(0.25, remaining)
            else:
                wait_for = 0.5
            if stop.wait(timeout=wait_for):
                reason = 'stopped'
                break
            if monitor._thread is None or not monitor._thread.is_alive():
                reason = 'monitor_exited'
                break
    except KeyboardInterrupt:
        reason = 'interrupted'
    finally:
        started.set()
        monitor.stop()
        if installed_sigterm:
            signal.signal(signal.SIGTERM, prev_sigterm)

    ok_exit = reason != 'monitor_exited'
    _emit_line(
        {'ok': ok_exit, 'event': 'daemon_stopped', 'reason': reason},
        f'监控守护进程已停止（reason={reason}）。',
    )
    return 0 if ok_exit else 1


def handle_watch(args: argparse.Namespace) -> int:
    """Validate workspace and dispatch `vasp-ctl watch` subcommands."""
    as_json = bool(getattr(args, 'as_json', False))
    try:
        workspace = Path(args.workspace).resolve(strict=True)
        if not workspace.is_dir():
            raise NotADirectoryError(f'工作区路径不是目录：{workspace.name}')
    except (OSError, ValueError) as exc:
        _emit_error(exc, as_json=as_json, default_code='INVALID_WORKSPACE')
        return 2
    try:
        if args.watch_action == 'once':
            return _handle_once(workspace, args)
        if args.watch_action == 'ack':
            return _handle_ack(workspace, args)
        if args.watch_action == 'daemon':
            if args.role != 'maker':
                return _emit_error(
                    core.ToolError('TOOL_NOT_ALLOWED_FOR_ROLE', '只读审查角色（checker）不能启动监控守护进程。'),
                    as_json=as_json,
                )
            return run_daemon(
                workspace,
                max_idle_wait=args.max_idle_wait,
                max_seconds=args.max_seconds,
                as_json=as_json,
            )
        return 2
    except (core.ToolError, event_outbox.EventOutboxError, MonitorPersistenceError) as exc:
        code = getattr(exc, 'error_code', 'WATCH_ERROR')
        rc = 2 if code == 'INVALID_ARGUMENTS' else 1
        _emit_error(exc, as_json=as_json, default_code=code)
        return rc
    except (OSError, ValueError) as exc:
        return _emit_error(exc, as_json=as_json, default_code='WATCH_ERROR')
