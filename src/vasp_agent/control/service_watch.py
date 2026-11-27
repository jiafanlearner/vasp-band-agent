"""Layer 4 persistent job-watch helpers for ControlService (`vasp_watch_jobs`) and `vasp-ctl watch`."""
from __future__ import annotations

from pathlib import Path
import re
from typing import Any

from vasp_agent.control import service_contracts
from vasp_agent.monitoring import event_outbox
from vasp_agent.monitoring.job_monitor import JobMonitor
from vasp_agent.ui.menu_text import table
from vasp_agent.workflow import stage_definitions
import vasp_agent.workflow.vasp_band_agent as core

_SERVER_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$')
_JOB_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')

_ACTIVE_JOB_FIELDS = (
    'server',
    'job_name',
    'phase',
    'job_id',
    'state',
    'terminal',
    'outcome',
    'workflow_status',
    'next_phase',
    'interval_seconds',
    'last_checked_at',
    'next_due_at',
    'error',
    'error_count',
    'array_progress',
)


def _compact_active_job(task: dict[str, Any]) -> dict[str, Any]:
    return {field: task.get(field) for field in _ACTIVE_JOB_FIELDS if field in task}


def _read_active_jobs(
    monitor: JobMonitor,
    *,
    server: str | None = None,
    job_name: str | None = None,
    phase: str | None = None,
) -> tuple[list[dict[str, Any]], bool]:
    """Read active jobs without creating runtime files when no monitor registry exists yet."""
    if not monitor.path.exists() and not monitor.path.is_symlink():
        return [], True
    status = monitor.status()
    jobs: list[dict[str, Any]] = []
    for task in status.get('tasks', []):
        if not task.get('active') or task.get('terminal'):
            continue
        if server is not None and task.get('server') != server:
            continue
        if job_name is not None and task.get('job_name') != job_name:
            continue
        if phase is not None and task.get('phase') != phase:
            continue
        jobs.append(_compact_active_job(task))
    return jobs, bool(status.get('enabled', True))


def watch_jobs(
    workspace: str | Path,
    *,
    monitor: JobMonitor | None = None,
    role: str = 'maker',
    actor_id: str = 'headless',
    poll: bool = False,
    ack_ids: list[str] | None = None,
    include_acked: bool = False,
    server: str | None = None,
    job_name: str | None = None,
    phase: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Read persistent monitor outbox events; Maker may also poll once and acknowledge events."""
    if role != 'maker':
        if poll:
            raise core.ToolError(
                'TOOL_NOT_ALLOWED_FOR_ROLE',
                '只读审查角色（checker）只能查看监控事件，不能触发远端轮询（poll=true）。',
            )
        if ack_ids is not None:
            raise core.ToolError(
                'TOOL_NOT_ALLOWED_FOR_ROLE',
                '只读审查角色（checker）不能确认（ack）监控事件。',
            )
    if type(limit) is not int or not 1 <= limit <= 200:
        raise core.ToolError('INVALID_ARGUMENTS', 'limit 必须为 1–200 的整数。')
    if server is not None and (not isinstance(server, str) or not _SERVER_RE.fullmatch(server)):
        raise core.ToolError('INVALID_ARGUMENTS', f'无效 server 过滤条件：{server!r}。')
    if job_name is not None and (not isinstance(job_name, str) or not _JOB_RE.fullmatch(job_name)):
        raise core.ToolError('INVALID_ARGUMENTS', f'无效 job_name 过滤条件：{job_name!r}。')
    if phase is not None and phase not in stage_definitions.known_names():
        raise core.ToolError('INVALID_ARGUMENTS', f'无效 phase 过滤条件：{phase!r}。')

    root = Path(workspace).resolve(strict=True)
    mon = monitor if monitor is not None else JobMonitor(root)

    ack_result: dict[str, Any] | None = None
    if ack_ids is not None:
        ack_result = event_outbox.ack_events(root, ack_ids, acked_by=f'{role}:{actor_id}')

    daemon_running = mon._owner is not None or event_outbox.worker_lock_held(root)
    polled = False
    polled_count = 0
    poll_skipped_reason: str | None = None

    if poll:
        if mon._owner is None and daemon_running:
            poll_skipped_reason = 'daemon_active'
        elif not mon.path.exists() and not mon.path.is_symlink():
            polled = True
            polled_count = 0
        else:
            checked = mon.check_once()
            if getattr(mon, '_last_check_skipped_locked', False):
                daemon_running = True
                poll_skipped_reason = 'worker_lock_busy'
            else:
                polled = True
                polled_count = len(checked)

    all_pending = event_outbox.list_events(
        root,
        include_acked=False,
        server=server,
        job_name=job_name,
        phase=phase,
    )
    pending_events = all_pending[:limit]
    active_jobs, monitor_enabled = _read_active_jobs(mon, server=server, job_name=job_name, phase=phase)
    intervals = [j['interval_seconds'] for j in active_jobs if isinstance(j.get('interval_seconds'), int)]
    suggested_poll = min(intervals) if intervals else None

    result: dict[str, Any] = {
        'autonomy_phase': service_contracts.AUTONOMY_PHASE,
        'submission_enabled': False,
        'monitor_enabled': monitor_enabled,
        'daemon_running': daemon_running,
        'polled': polled,
        'polled_count': polled_count,
        'poll_skipped_reason': poll_skipped_reason,
        'ack_result': ack_result,
        'pending_count': len(all_pending),
        'pending_events': pending_events,
        'active_jobs': active_jobs,
        'suggested_poll_seconds': suggested_poll,
    }
    if include_acked:
        result['acked_events'] = event_outbox.list_events(
            root,
            only_acked=True,
            server=server,
            job_name=job_name,
            phase=phase,
            limit=limit,
        )
    return result


def render_watch_summary(payload: dict[str, Any]) -> str:
    """Render a concise tabular watch summary for human CLI output."""
    poll_text = (
        f"已轮询 {payload['polled_count']} 个作业"
        if payload.get('polled')
        else (f"跳过轮询（{payload['poll_skipped_reason']}）" if payload.get('poll_skipped_reason') else '仅读取队列（未轮询）')
    )
    sections = [
        '作业监控事件队列总览',
        table([
            ['守护进程状态', '运行中（持有 worker_lock）' if payload.get('daemon_running') else '未运行'],
            ['本次轮询', poll_text],
            ['待确认事件', str(payload.get('pending_count', 0))],
            ['活跃作业数', str(len(payload.get('active_jobs') or []))],
            ['建议下次轮询', f"{payload['suggested_poll_seconds']}s" if payload.get('suggested_poll_seconds') else '-'],
        ]),
    ]
    pending = payload.get('pending_events') or []
    if pending:
        rows = [['事件 ID', '类型', '目标任务', '阶段', 'JobID', '状态', '下一阶段', '时间（UTC）']]
        for ev in pending:
            rows.append([
                ev.get('event_id', '-'),
                ev.get('kind', '-'),
                f"{ev.get('server')}/{ev.get('job_name')}",
                ev.get('phase') or '-',
                ev.get('job_id', '-'),
                ev.get('state', '-'),
                ev.get('next_phase') or '-',
                ev.get('occurred_at', '-'),
            ])
        sections.append('【待确认事件】\n' + table(rows))
    else:
        sections.append('【待确认事件】\n  （暂无待确认事件）')

    active = payload.get('active_jobs') or []
    if active:
        rows = [['目标任务', '阶段', 'JobID', '状态', '间隔', '连续错误']]
        for job in active:
            rows.append([
                f"{job.get('server')}/{job.get('job_name')}",
                job.get('phase') or '-',
                job.get('job_id', '-'),
                job.get('state', '-'),
                f"{job.get('interval_seconds', '-')}s",
                str(job.get('error_count', 0)),
            ])
        sections.append('【活跃作业】\n' + table(rows))
    return '\n\n'.join(sections)
