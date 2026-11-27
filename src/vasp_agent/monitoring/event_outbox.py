"""Layer 3 persistent monitor event outbox with idempotent dedupe, ack cursor, and FileLock safety.

Zero upward imports into chat/ or control/; reuses JobMonitor's state_lock and worker_lock.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import threading
from typing import Any
from uuid import uuid4

from vasp_agent.common import agent_paths
from vasp_agent.common.file_lock import FileLock, FileLockContentionError, FileLockError
from vasp_agent.workflow import stage_definitions

OUTBOX_VERSION = 1
OUTBOX_KINDS = ('terminal', 'error', 'recovered')
MAX_ACKED_HISTORY = 500
MAX_OUTBOX_BYTES = 5 * 1024 * 1024

_EVENT_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{3,63}$')
_SERVER_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,47}$')
_JOB_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$')
_JOB_ID_RE = re.compile(r'^[0-9]+$')

_STATE_LOCKS_GUARD = threading.Lock()
_STATE_LOCKS: dict[str, threading.RLock] = {}


def state_thread_lock(workspace: str | Path) -> threading.RLock:
    """Process-local reentrant lock per workspace serializing threads before OS state_lock."""
    key = str(Path(workspace).resolve(strict=True))
    with _STATE_LOCKS_GUARD:
        lock = _STATE_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _STATE_LOCKS[key] = lock
        return lock


class EventOutboxError(FileLockError):
    """Structured persistence/validation error for the monitor event outbox."""

    def __init__(self, error_code: str, message: str | None = None):
        if message is None:
            message = str(error_code)
            error_code = 'OUTBOX_PERSISTENCE_ERROR'
        super().__init__(message)
        self.error_code = error_code


class EventOutboxLockError(EventOutboxError, FileLockContentionError):
    """Transient lock contention when acquiring monitor state/worker lock."""

    def __init__(self, message: str = '无法获取监控事件队列锁，请稍后重试。'):
        super().__init__('OUTBOX_LOCK_CONTENTION', message)


def _to_utc(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _format_utc(now: datetime | None = None) -> str:
    return _to_utc(now).strftime('%Y-%m-%dT%H:%M:%SZ')


def _safe_text(value: Any, *, limit: int = 240) -> str | None:
    if value is None:
        return None
    from vasp_agent.ui.terminal_ui import display_safe

    cleaned = ' '.join(str(display_safe(str(value))).split())
    return cleaned[:limit] if cleaned else ''


def _safe_array_progress(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, Any] = {}
    for key, item in list(value.items())[:12]:
        if isinstance(key, str) and (item is None or isinstance(item, (int, float, bool, str))):
            result[key[:40]] = _safe_text(item, limit=80) if isinstance(item, str) else item
    return result


def _validate_path_no_symlinks(root: Path, target: Path, label: str) -> Path:
    try:
        rel = target.relative_to(root)
    except ValueError as exc:
        raise EventOutboxError('OUTBOX_PATH_ESCAPE', f'{label}超出工作区边界。') from exc
    cursor = root
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise EventOutboxError('OUTBOX_SYMLINK_REFUSED', f'{label}不能包含符号链接：{rel.as_posix()}。')
    if target.exists():
        resolved = target.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise EventOutboxError('OUTBOX_PATH_ESCAPE', f'{label}解析后超出工作区边界。')
        if target.is_dir():
            raise EventOutboxError('OUTBOX_CORRUPTED', f'{label}不能是目录。')
    return target


def outbox_path(workspace: str | Path) -> Path:
    """Resolve and symlink-check the workspace monitor event outbox JSONL file."""
    root = Path(workspace).resolve(strict=True)
    target = agent_paths.file_path(root, 'monitor_events')
    return _validate_path_no_symlinks(root, target, '监控事件队列')


def worker_lock_held(workspace: str | Path) -> bool:
    """Return True if another process/monitor currently holds the JobMonitor worker_lock."""
    root = Path(workspace).resolve(strict=True)
    path = agent_paths.monitor_paths(root)['worker_lock']
    _validate_path_no_symlinks(root, path, '监控工作锁')
    if not path.exists():
        return False
    lock = FileLock(
        path,
        blocking=False,
        error_type=EventOutboxError,
        contention_error_type=EventOutboxLockError,
    )
    if lock.acquire():
        lock.release()
        return False
    return True


@contextmanager
def _locked_state(workspace: str | Path):
    """Acquire process-local RLock then JobMonitor's shared OS state_lock."""
    root = Path(workspace).resolve(strict=True)
    with state_thread_lock(root):
        for _ in range(5):
            paths = agent_paths.monitor_paths(root)
            lock_file = _validate_path_no_symlinks(root, paths['state_lock'], '监控状态锁')
            lock_file.parent.mkdir(parents=True, exist_ok=True)
            lock = FileLock(
                lock_file,
                blocking=True,
                error_type=EventOutboxError,
                contention_error_type=EventOutboxLockError,
            )
            lock.acquire()
            if agent_paths.monitor_paths(root)['registry'] == paths['registry']:
                try:
                    yield root
                finally:
                    lock.release()
                return
            lock.release()
        raise EventOutboxError('OUTBOX_LOCK_CONTENTION', '监控记录位置正在变化；请稍后重试。')


def _validate_identity(server: Any, job_name: Any, phase: Any, job_id: Any) -> tuple[str, str, str | None, str]:
    job_id_str = str(job_id) if job_id is not None else ''
    if (
        not isinstance(server, str)
        or not _SERVER_RE.fullmatch(server)
        or not isinstance(job_name, str)
        or not _JOB_RE.fullmatch(job_name)
        or (phase is not None and phase not in stage_definitions.known_names())
        or not _JOB_ID_RE.fullmatch(job_id_str)
    ):
        raise EventOutboxError('OUTBOX_INVALID_EVENT', '监控事件缺少有效的 server、job_name、phase 或 job_id。')
    return server, job_name, phase, job_id_str


def dedupe_key(
    server: str,
    job_name: str,
    phase: str | None,
    job_id: str,
    kind: str,
    state: str,
) -> str:
    """Build an injective idempotency key from (server, job_name, phase, job_id, kind, state)."""
    srv, jname, ph, jid = _validate_identity(server, job_name, phase, job_id)
    if kind not in OUTBOX_KINDS:
        raise EventOutboxError('OUTBOX_INVALID_EVENT', f'不支持的持久化事件类型：{kind!r}。')
    clean_state = _safe_text(state, limit=80) or 'UNKNOWN'
    return f'{srv}|{jname}|{ph or ""}|{jid}|{kind}|{clean_state}'


def _parse_record(line: str, line_no: int) -> dict[str, Any]:
    try:
        row = json.loads(line)
    except ValueError as exc:
        raise EventOutboxError('OUTBOX_CORRUPTED', f'监控事件队列第 {line_no} 行 JSON 已损坏，拒绝覆盖。') from exc
    if (
        not isinstance(row, dict)
        or row.get('version') != OUTBOX_VERSION
        or not isinstance(row.get('event_id'), str)
        or not _EVENT_ID_RE.fullmatch(row['event_id'])
        or not isinstance(row.get('dedupe_key'), str)
        or not row['dedupe_key']
        or row.get('kind') not in OUTBOX_KINDS
        or not isinstance(row.get('occurred_at'), str)
        or (row.get('acked_at') is not None and not isinstance(row.get('acked_at'), str))
        or (row.get('acked_by') is not None and not isinstance(row.get('acked_by'), str))
    ):
        raise EventOutboxError('OUTBOX_CORRUPTED', f'监控事件队列第 {line_no} 行结构无效，拒绝覆盖。')
    _validate_identity(row.get('server'), row.get('job_name'), row.get('phase'), row.get('job_id'))
    return row


def _read_records_locked(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        if path.stat().st_size > MAX_OUTBOX_BYTES:
            raise EventOutboxError('OUTBOX_CORRUPTED', '监控事件队列体积超过安全上限，拒绝读取或覆盖。')
        raw = path.read_text(encoding='utf-8')
    except EventOutboxError:
        raise
    except (OSError, UnicodeDecodeError) as exc:
        raise EventOutboxError('OUTBOX_CORRUPTED', '无法读取监控事件队列，拒绝覆盖。') from exc
    records: list[dict[str, Any]] = []
    for line_no, line in enumerate(raw.splitlines(), 1):
        if not line.strip():
            continue
        records.append(_parse_record(line, line_no))
    return records


def _atomic_write_records_locked(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f'.{path.name}.{uuid4().hex}.tmp'
    payload = ''.join(json.dumps(row, ensure_ascii=False, separators=(',', ':')) + '\n' for row in records)
    try:
        with temp.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    except OSError as exc:
        raise EventOutboxError('OUTBOX_WRITE_FAILED', '无法原子更新监控事件队列。') from exc
    finally:
        temp.unlink(missing_ok=True)


def preflight_locked(workspace: str | Path) -> Path:
    """Validate outbox path (no symlinks) and existing JSONL integrity before committing registry writes."""
    path = outbox_path(workspace)
    _read_records_locked(path)
    return path


def _new_event_id(now: datetime | None = None) -> str:
    return f"evt-{_to_utc(now).strftime('%Y%m%d')}-{uuid4().hex[:12]}"


def append_event_locked(
    workspace: str | Path,
    event: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Append one changed terminal/error/recovered event while caller already holds state_lock."""
    if not isinstance(event, dict) or not event.get('changed'):
        return None
    kind = event.get('kind')
    if kind not in OUTBOX_KINDS:
        return None
    path = outbox_path(workspace)
    srv, jname, ph, jid = _validate_identity(
        event.get('server'), event.get('job_name'), event.get('phase'), event.get('job_id'),
    )
    clean_state = _safe_text(event.get('state'), limit=80) or 'UNKNOWN'
    key = dedupe_key(srv, jname, ph, jid, kind, clean_state)

    existing = _read_records_locked(path)
    if kind == 'terminal':
        for row in existing:
            if row['dedupe_key'] == key:
                return dict(row)
    else:
        same_job = [
            row for row in existing
            if (row['server'], row['job_name'], row.get('phase'), row['job_id']) == (srv, jname, ph, jid)
        ]
        if same_job and same_job[-1]['dedupe_key'] == key:
            return dict(same_job[-1])

    raw_error_count = event.get('error_count', 0)
    if raw_error_count is None:
        error_count = 0
    elif type(raw_error_count) is bool or not isinstance(raw_error_count, int) or raw_error_count < 0:
        raise EventOutboxError('OUTBOX_INVALID_EVENT', f'无效的 error_count：{raw_error_count!r}。')
    else:
        error_count = raw_error_count

    record: dict[str, Any] = {
        'version': OUTBOX_VERSION,
        'event_id': _new_event_id(now),
        'dedupe_key': key,
        'server': srv,
        'job_name': jname,
        'phase': ph,
        'job_id': jid,
        'kind': kind,
        'state': clean_state,
        'terminal': bool(event.get('terminal')),
        'outcome': _safe_text(event.get('outcome'), limit=64),
        'workflow_status': _safe_text(event.get('workflow_status'), limit=64),
        'next_phase': _safe_text(event.get('next_phase'), limit=64),
        'error': _safe_text(event.get('error'), limit=80),
        'error_count': error_count,
        'array_progress': _safe_array_progress(event.get('array_progress')),
        'message': _safe_text(event.get('message'), limit=240),
        'occurred_at': _format_utc(now),
        'acked_at': None,
        'acked_by': None,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=False, separators=(',', ':')) + '\n'
    try:
        with path.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(line)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise EventOutboxError('OUTBOX_WRITE_FAILED', '无法写入监控事件队列。') from exc
    return dict(record)


def append_event(
    workspace: str | Path,
    event: dict[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Acquire JobMonitor's shared state_lock and append one actionable monitor event."""
    if not isinstance(event, dict) or not event.get('changed') or event.get('kind') not in OUTBOX_KINDS:
        return None
    with _locked_state(workspace) as root:
        return append_event_locked(root, event, now=now)


def list_events(
    workspace: str | Path,
    *,
    include_acked: bool = False,
    only_acked: bool = False,
    server: str | None = None,
    job_name: str | None = None,
    phase: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Read persisted monitor events without creating runtime directories when absent."""
    root = Path(workspace).resolve(strict=True)
    with state_thread_lock(root):
        path = outbox_path(root)
        if not path.exists():
            return []
        with _locked_state(root):
            records = _read_records_locked(path)
    filtered: list[dict[str, Any]] = []
    for row in records:
        is_acked = row.get('acked_at') is not None
        if only_acked and not is_acked:
            continue
        if not include_acked and not only_acked and is_acked:
            continue
        if server is not None and row['server'] != server:
            continue
        if job_name is not None and row['job_name'] != job_name:
            continue
        if phase is not None and row.get('phase') != phase:
            continue
        filtered.append(dict(row))
    if limit is not None:
        if limit < 0:
            raise EventOutboxError('INVALID_ARGUMENTS', 'limit 不能为负数。')
        return filtered[-limit:] if (only_acked and limit > 0) else filtered[:limit]
    return filtered


def ack_events(
    workspace: str | Path,
    event_ids: list[str] | tuple[str, ...] | None = None,
    *,
    ack_all: bool = False,
    acked_by: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Idempotently acknowledge events by ID (or all pending events) under state_lock."""
    if ack_all and event_ids:
        raise EventOutboxError('INVALID_ARGUMENTS', '不能同时传入 event_ids 与 ack_all。')
    unique_ids: list[str] = []
    if not ack_all:
        if not isinstance(event_ids, (list, tuple)) or not event_ids:
            raise EventOutboxError('INVALID_ARGUMENTS', '需要提供至少一个有效 event_id，或显式使用 ack_all。')
        seen: set[str] = set()
        for item in event_ids:
            if not isinstance(item, str) or not _EVENT_ID_RE.fullmatch(item):
                raise EventOutboxError('INVALID_ARGUMENTS', f'无效事件 ID：{item!r}。')
            if item not in seen:
                seen.add(item)
                unique_ids.append(item)

    root = Path(workspace).resolve(strict=True)
    ts = _format_utc(now)
    actor = _safe_text(acked_by, limit=80)
    with state_thread_lock(root):
        path = outbox_path(root)
        if not path.exists():
            return {
                'acked': [],
                'already_acked': [],
                'not_found': list(unique_ids),
                'pending_count': 0,
            }
        with _locked_state(root):
            records = _read_records_locked(path)
            by_id = {row['event_id']: row for row in records}
            acked: list[str] = []
            already_acked: list[str] = []
            not_found: list[str] = []

            if ack_all:
                for row in records:
                    if row.get('acked_at') is None:
                        row['acked_at'] = ts
                        row['acked_by'] = actor
                        acked.append(row['event_id'])
            else:
                for eid in unique_ids:
                    row = by_id.get(eid)
                    if row is None:
                        not_found.append(eid)
                    elif row.get('acked_at') is not None:
                        already_acked.append(eid)
                    else:
                        row['acked_at'] = ts
                        row['acked_by'] = actor
                        acked.append(eid)

            if acked:
                acked_rows = [row for row in records if row.get('acked_at') is not None]
                if len(acked_rows) > MAX_ACKED_HISTORY:
                    drop_ids = {row['event_id'] for row in acked_rows[:-MAX_ACKED_HISTORY]}
                    records = [row for row in records if row['event_id'] not in drop_ids]
                _atomic_write_records_locked(path, records)

            pending_count = sum(1 for row in records if row.get('acked_at') is None)
    return {
        'acked': acked,
        'already_acked': already_acked,
        'not_found': not_found,
        'pending_count': pending_count,
    }
