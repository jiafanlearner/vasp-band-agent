"""Small durable scheduler watch list; no model calls or submission capabilities."""
from copy import deepcopy
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from vasp_agent.common import agent_paths
from vasp_agent.common.file_lock import FileLock as _CommonFileLock, FileLockContentionError, FileLockError
from vasp_agent.monitoring import event_outbox
from vasp_agent.workflow import stage_definitions


class MonitorPersistenceError(FileLockError):
    pass


class MonitorLockError(MonitorPersistenceError, FileLockContentionError):
    """Transient lock acquisition failure; safe for the background loop to back off and retry."""


def interval(value):
    if type(value) is not int or not 60 <= value <= 86400:
        raise ValueError('检查间隔必须为 60–86400 秒。')
    return value


def suggested_interval(resources, baseline=900):
    """Coarse scheduling heuristic, not a prediction of VASP execution time."""
    match = re.fullmatch(r'(\d{1,4}):([0-5]\d):([0-5]\d)', str(resources.get('walltime', '')))
    if match:
        seconds = int(match[1]) * 3600 + int(match[2]) * 60 + int(match[3])
        if 0 < seconds <= 3600:
            return 300
        if seconds >= 8 * 3600:
            return 1800
    return baseline


def safe_message(value):
    # Persist only a bounded display message, never complete RPC results/logs.
    from vasp_agent.ui.terminal_ui import display_safe
    return ' '.join(str(display_safe(str(value or ''))).split())[:240]


class FileLock(_CommonFileLock):
    """Re-exported OS file lock raising MonitorPersistenceError/MonitorLockError."""

    def __init__(self, path, blocking=True):
        super().__init__(
            path,
            blocking=blocking,
            error_type=MonitorPersistenceError,
            contention_error_type=MonitorLockError,
        )


MAX_IDLE_WAIT = 30.0
MIN_IDLE_WAIT = 0.05
MAX_LOCK_BACKOFF = 30.0


class JobMonitor:
    def __init__(self, workspace, clock=None, max_idle_wait=MAX_IDLE_WAIT):
        self.workspace = Path(workspace).resolve(strict=True)
        self.clock = clock or time.time
        self.max_idle_wait = max_idle_wait
        self._mutex = event_outbox.state_thread_lock(self.workspace)
        self._poll_mutex = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._next_wait = max_idle_wait
        self._thread = None
        self._owner = None
        self._poller = self._notify = None
        self._last_check_skipped_locked = False

    @property
    def path(self):
        """The registry in force; see agent_paths.monitor_paths for the precedence."""
        return agent_paths.monitor_paths(self.workspace)['registry']

    def _lock(self, key, blocking=True):
        path = agent_paths.monitor_paths(self.workspace)[key]
        path.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(path, blocking=blocking)

    def _locked_set(self):
        """Lock the registry set in force; re-resolve if a migration moved it meanwhile."""
        for _ in range(5):
            paths = agent_paths.monitor_paths(self.workspace)
            paths['state_lock'].parent.mkdir(parents=True, exist_ok=True)
            lock = FileLock(paths['state_lock'])
            lock.acquire()
            if agent_paths.monitor_paths(self.workspace)['registry'] == paths['registry']:
                return paths, lock
            lock.release()
        raise MonitorPersistenceError('监控记录位置正在变化；请稍后重试。')

    def _load(self, path=None):
        path = path or self.path
        if path.is_symlink():
            raise MonitorPersistenceError('监控记录不能是符号链接。')
        if not path.exists():
            return {'version': 1, 'enabled': True, 'auto': True, 'interval_seconds': 900, 'tasks': []}
        try:
            if path.stat().st_size > 1_000_000:
                raise ValueError()
            data = json.loads(path.read_text(encoding='utf-8'))
            if (data.get('version') != 1 or type(data.get('enabled')) is not bool
                    or type(data.get('auto')) is not bool or not isinstance(data.get('tasks'), list)):
                raise ValueError()
            interval(data['interval_seconds'])
            for task in data['tasks']:
                self._identity(task['server'], task['job_name'], task.get('phase'), task['job_id'])
                interval(task['interval_seconds'])
                if not math.isfinite(task['next_due_at']):
                    raise ValueError()
            return data
        except (ValueError, KeyError, TypeError, AttributeError, OSError):
            raise MonitorPersistenceError('监控记录无效或无法读取；不会覆盖现有记录。') from None

    @contextmanager
    def _transaction(self, write=False, before_write=None, on_committed=None):
        with self._mutex:
            paths, lock = self._locked_set()
            try:
                data = self._load(paths['registry'])
                yield data
                if write:
                    if before_write is not None:
                        before_write()
                    temp = paths['temp_dir'] / (paths['temp_prefix'] + uuid4().hex + '.tmp')
                    try:
                        with temp.open('x', encoding='utf-8', newline='\n') as stream:
                            json.dump(data, stream, ensure_ascii=False, indent=2)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.replace(temp, paths['registry'])
                    except OSError:
                        raise MonitorPersistenceError('无法保存监控记录；已提交作业不受影响。') from None
                    finally:
                        temp.unlink(missing_ok=True)
                    if on_committed is not None:
                        on_committed()
            finally:
                lock.release()

    @staticmethod
    def _identity(server, name, phase, job_id):
        if (not isinstance(server, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,47}', server)
                or not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', name)
                or phase is not None and phase not in stage_definitions.known_names()
                or not re.fullmatch(r'[0-9]+', str(job_id))):
            raise ValueError('监控需要实际服务器、任务、阶段及有效 JobID。')
        return (server, name, phase, str(job_id))

    def status(self):
        with self._transaction() as data:
            result = deepcopy(data)
        result.update(running=bool(self._thread and self._thread.is_alive()), owns_polling=self._owner is not None)
        return result

    def register(self, server, job_name, phase, job_id, resources=None, interval_seconds=None, reason=None,
                 conversation=None):
        key = self._identity(server or 'local', job_name, phase, job_id)
        if interval_seconds is not None:
            interval(interval_seconds)
        resources = {k: v for k, v in (resources or {}).items()
                     if k in ('nodes', 'parallel_tests', 'cores_per_test', 'walltime', 'submission_mode')
                     and type(v) in (str, int)}
        with self._transaction(write=True) as data:
            existing = next((t for t in data['tasks'] if self._identity(t['server'], t['job_name'], t.get('phase'), t['job_id']) == key), None)
            if existing:
                return deepcopy(existing)
            chosen = (interval_seconds or suggested_interval(resources, data['interval_seconds'])) if data['auto'] else data['interval_seconds']
            task = dict(server=key[0], job_name=job_name, phase=phase, job_id=key[3],
                        resources=resources, recommendation_seconds=interval_seconds,
                        reason=safe_message(reason or '依据运行时限粗略调整；不是实际耗时预测。'),
                        interval_seconds=chosen, active=True, enabled=True, state='UNQUERIED', terminal=False,
                        outcome=None, last_checked_at=None, next_due_at=self.clock() + chosen, error_count=0)
            # The conversation that submitted the job; a finished job's follow-up
            # belongs there, not in whichever conversation happens to be open.
            if isinstance(conversation, str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]{0,127}', conversation):
                task['conversation'] = conversation
            data['tasks'].append(task)
            # Bound finished history while preserving all active submissions.
            finished = [t for t in data['tasks'] if not t.get('active')]
            for old in finished[:-100]:
                data['tasks'].remove(old)
        self._wake.set()
        return deepcopy(task)

    def configure(self, enabled=None, interval_seconds=None, auto=None):
        if enabled is not None and type(enabled) is not bool or auto is not None and type(auto) is not bool:
            raise ValueError('监控开关必须是布尔值。')
        if interval_seconds is not None:
            interval(interval_seconds)
        with self._transaction(write=True) as data:
            if enabled is not None:
                data['enabled'] = enabled
            if auto is not None:
                data['auto'] = auto
            if interval_seconds is not None:
                data['interval_seconds'] = interval_seconds
            if auto is not None or interval_seconds is not None:
                for task in data['tasks']:
                    if not task.get('active'):
                        continue
                    task['interval_seconds'] = ((task.get('recommendation_seconds') or suggested_interval(task.get('resources', {}), data['interval_seconds']))
                                                if data['auto'] else data['interval_seconds'])
                    task['next_due_at'] = self.clock() + task['interval_seconds']
        self._wake.set()
        return self.status()

    def deactivate(self, server, job_name, phase, job_id, state='CANCELLED'):
        """Stop polling one submission whose receipt was released; keep it as history."""
        key = self._identity(server or 'local', job_name, phase, job_id)
        with self._transaction(write=True) as data:
            matched = [t for t in data['tasks'] if self._identity(t['server'], t['job_name'], t.get('phase'), t['job_id']) == key]
            for task in matched:
                task.update(active=False, terminal=True, state=state, outcome='submission_released')
        return bool(matched)

    def remove(self, job_id, server=None):
        if not re.fullmatch(r'[0-9]+', str(job_id)):
            raise ValueError('请输入有效 JobID。')
        with self._transaction(write=True) as data:
            matched = [t for t in data['tasks'] if t['job_id'] == str(job_id) and (server is None or t['server'] == server)]
            if len({t['server'] for t in matched}) > 1:
                raise ValueError('多个服务器使用相同 JobID；请指定服务器。')
            for task in matched:
                data['tasks'].remove(task)
        return bool(matched)

    def start(self, poller, notify):
        self._poller, self._notify = poller, notify
        if self._thread and self._thread.is_alive():
            return True
        owner = self._lock('worker_lock', blocking=False)
        if not owner.acquire():
            return False
        self._owner = owner
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name='vasp-job-monitor', daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            self._thread.join(timeout=1)

    def _emit(self, event):
        if self._notify and not self._stop.is_set():
            try:
                self._notify(event)
            except Exception:
                # Display failures cannot affect the saved receipt or next check.
                pass

    def _compute_wait(self, data):
        if not data.get('enabled', True):
            return self.max_idle_wait
        dues = [t['next_due_at'] for t in data.get('tasks', []) if t.get('active') and t.get('enabled', True)]
        if not dues:
            return self.max_idle_wait
        remaining = min(dues) - self.clock()
        return max(MIN_IDLE_WAIT, min(self.max_idle_wait, remaining))

    def _run(self):
        lock_failures = 0
        try:
            while not self._stop.is_set():
                self._wake.clear()
                try:
                    self._check(force=False)
                except MonitorLockError:
                    lock_failures += 1
                    backoff = min(MAX_LOCK_BACKOFF, 0.5 * 2 ** min(lock_failures - 1, 5))
                    self._wake.wait(timeout=backoff)
                    continue
                except Exception:
                    # A damaged registry must not crash the conversational CLI or cause writes.
                    self._emit({'kind': 'error', 'message': '监控记录无法读取或保存；周期检查已停止，请用 /watch 检查。'})
                    break
                lock_failures = 0
                self._wake.wait(timeout=self._next_wait)
        finally:
            owner, self._owner = self._owner, None
            if owner:
                owner.release()

    def check_once(self):
        if self._poller is None:
            from vasp_agent.monitoring.polling import poll_job
            self._poller = lambda task: poll_job(self.workspace, task)
        if self._owner is not None:
            self._last_check_skipped_locked = False
            return self._check(force=True)
        owner = self._lock('worker_lock', blocking=False)
        if not owner.acquire():
            self._last_check_skipped_locked = True
            return []
        self._last_check_skipped_locked = False
        try:
            return self._check(force=True)
        finally:
            owner.release()

    def _check(self, force):
        if not self._poll_mutex.acquire(blocking=False):
            return []
        events = []
        try:
            with self._transaction() as data:
                self._next_wait = self._compute_wait(data)
                tasks = [deepcopy(t) for t in data['tasks'] if t.get('active') and t.get('enabled', True)
                         and (force or (data['enabled'] and t['next_due_at'] <= self.clock()))]
            for target in tasks:
                if not force and self._stop.is_set():
                    break
                with self._transaction() as current:
                    live = next((t for t in current['tasks'] if (t['server'], t['job_name'], t.get('phase'), t['job_id'])
                                 == (target['server'], target['job_name'], target.get('phase'), target['job_id'])), None)
                    if live is None or not live.get('active') or not live.get('enabled', True):
                        continue
                    if not force and (not current['enabled'] or live['next_due_at'] > self.clock()):
                        continue
                try:
                    result = self._poller(deepcopy(target))
                    if (not isinstance(result, dict) or type(result.get('terminal')) is not bool
                            or not isinstance(result.get('state'), str)):
                        raise ValueError('监控状态响应无效。')
                    error = None
                except Exception as exc:
                    code = getattr(exc, 'error_code', type(exc).__name__)
                    result = {'message': f'只读查询失败（{safe_message(code)}）；连接或调度服务恢复后重试。'}
                    error = str(code)
                outbox_pending = [None]
                with self._transaction(
                    write=True,
                    before_write=lambda: (
                        event_outbox.preflight_locked(self.workspace)
                        if outbox_pending[0] is not None else None
                    ),
                    on_committed=lambda: (
                        event_outbox.append_event_locked(self.workspace, outbox_pending[0])
                        if outbox_pending[0] is not None else None
                    ),
                ) as data:
                    key = self._identity(target['server'], target['job_name'], target.get('phase'), target['job_id'])
                    task = next((t for t in data['tasks'] if self._identity(t['server'], t['job_name'], t.get('phase'), t['job_id']) == key), None)
                    if task is None or not task.get('active'):
                        continue
                    previous = {k: task.get(k) for k in ('state', 'terminal', 'outcome', 'workflow_status', 'next_phase', 'array_progress')}
                    old_error = task.get('error')
                    task['last_checked_at'] = self.clock()
                    if error:
                        task.update(error=error, error_count=task.get('error_count', 0) + 1, message=result['message'])
                        changed = old_error != error
                        kind = 'error'
                        delay = min(86400, task['interval_seconds'] * 2 ** min(task['error_count'] - 1, 4))
                    else:
                        for field in ('state', 'terminal', 'outcome', 'workflow_status', 'next_phase', 'message', 'array_progress'):
                            task[field] = safe_message(result[field]) if field in ('state', 'workflow_status', 'next_phase', 'message') and result.get(field) is not None else result.get(field)
                        task.update(error=None, error_count=0)
                        if task['terminal']:
                            task['active'] = False
                        changed = bool(old_error) or previous != {k: task.get(k) for k in previous}
                        kind = 'terminal' if task['terminal'] else 'recovered' if old_error else 'status'
                        delay = task['interval_seconds']
                    task['next_due_at'] = self.clock() + delay
                    self._next_wait = self._compute_wait(data)
                    event = {**deepcopy(task), 'changed': changed, 'kind': kind}
                    if changed and kind in event_outbox.OUTBOX_KINDS:
                        outbox_pending[0] = event
                    notify_enabled = data['enabled']
                events.append(event)
                # A manual check prints its own result, but a terminal state it finds
                # must still reach listeners: the task is deactivated here, so no
                # later background check would ever report it.
                if changed and ((notify_enabled and not force) or (force and kind == 'terminal')):
                    self._emit({**event, 'manual': force})
            return events
        finally:
            self._poll_mutex.release()


def status_line(monitor, now=None):
    """One short line about background monitoring for the prompt's status bar ('' when idle)."""
    from datetime import datetime
    try:
        data = monitor.status()
    except Exception:
        return '监控状态暂时无法读取'
    tasks = [task for task in data.get('tasks', []) if task.get('active') and not task.get('terminal')]
    if not tasks:
        return ''
    if not data.get('enabled', True):
        return f'监控已暂停 · {len(tasks)} 个作业未在检查（/watch on 开启）'
    if not data.get('running'):
        return f'监控未运行 · {len(tasks)} 个作业（/watch check 可手动检查）'
    def clock(value):
        return datetime.fromtimestamp(value).strftime('%H:%M') if isinstance(value, (int, float)) else '—'
    task = min(tasks, key=lambda t: t.get('next_due_at') or float('inf'))
    state = task.get('state') or 'UNQUERIED'
    state = '等待首次查询' if state == 'UNQUERIED' else state
    phase = f"（{task['phase']}）" if task.get('phase') else ''
    more = f' 等 {len(tasks)} 个作业' if len(tasks) > 1 else ''
    checked = f"上次 {clock(task.get('last_checked_at'))} · " if task.get('last_checked_at') else ''
    errors = f" · 连续读取失败 {task['error_count']} 次" if task.get('error_count') else ''
    return (f"监控中 · {task.get('job_name')}{phase} {state}{more} · {checked}"
            f"下次检查 {clock(task.get('next_due_at'))}{errors}")
