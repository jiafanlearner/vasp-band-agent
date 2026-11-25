"""Safe incremental directory naming when a forked task name already exists.

Never overwrites an existing task directory: detects collisions proactively
(local runs/ or remote list_jobs) and reactively (on engine/worker refusal),
proposes <base>_2, <base>_3, ... within the 64-character JOB regex limit, and
requires operator confirmation before creating the new directory.
"""
from __future__ import annotations

import re

JOB_NAME_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
DIR_EXISTS_MARKERS = ('已存在', '不存在的新目录', '不存在的新任务目录')
MAX_SUFFIX = 999


def is_dir_exists_error(exc: BaseException) -> bool:
    """True when a fork preparation refused because the target directory already exists."""
    if not isinstance(exc, ValueError):
        return False
    if getattr(exc, 'error_code', 'REMOTE_ERROR') not in ('REMOTE_ERROR', 'TOOL_ERROR'):
        return False
    message = str(exc)
    return any(marker in message for marker in DIR_EXISTS_MARKERS)


def with_suffix(base_name: str, index: int) -> str:
    """Attach _2, _3, ... while keeping the result <= 64 chars and JOB-valid."""
    if index <= 1:
        name = base_name[:64]
    else:
        suffix = f'_{index}'
        name = base_name[:64 - len(suffix)] + suffix
    if not JOB_NAME_RE.fullmatch(name):
        raise ValueError(f'无法生成合法任务目录名：{name}')
    return name


def existing_jobs(box, server) -> set[str]:
    """Best-effort set of existing job directory names on local or remote workspace."""
    if server is None:
        runs = box.workspace / 'runs'
        if not runs.is_dir():
            return set()
        try:
            return {p.name for p in runs.iterdir()}
        except OSError:
            return set()
    try:
        from vasp_agent.remote.remote_compute import RemoteCompute
        reply = RemoteCompute(box.workspace, server).call('list_jobs', include_archived=True)
    except Exception:
        return set()
    jobs = reply.get('jobs') if isinstance(reply, dict) else None
    if not isinstance(jobs, list):
        return set()
    return {item['job_name'] for item in jobs if isinstance(item, dict) and isinstance(item.get('job_name'), str)}


def resolve_name(base_name: str, taken: set[str]) -> tuple[str, bool]:
    """Return (available_name, conflicted) without overwriting any name in taken."""
    if base_name not in taken:
        return base_name, False
    for index in range(2, MAX_SUFFIX + 1):
        candidate = with_suffix(base_name, index)
        if candidate not in taken:
            return candidate, True
    raise ValueError(f'目录名 {base_name} 的递增后缀已用尽，请清理或改用其他参数。')


def conflict_notice(existing_name: str, new_name: str) -> str:
    return f'目录 {existing_name} 已存在（不会覆盖原目录）；将改用新目录名 {new_name}。'


def confirm_reactive_increment(base_name: str, failed_name: str, taken: set[str], reader, output) -> str | None:
    """Handle a directory-exists refusal at prepare time by asking the operator to confirm _2/_3."""
    taken.add(failed_name)
    candidate, _ = resolve_name(base_name, taken)
    output(conflict_notice(failed_name, candidate))
    if reader(f'确认改用新目录名 {candidate} 请输入 1；其他输入取消：').strip() == '1':
        return candidate
    return None
