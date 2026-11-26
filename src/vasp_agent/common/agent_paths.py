"""Where the chat agent keeps its own runtime files inside a workspace.

Everything the agent writes for itself lives under <workspace>/.agent/:

    sessions/           ai-session-<name>-<identity>.json and its sidecars
    sessions/archive/   archived sessions (moved there, never deleted)
    audit/              ai-audit.jsonl (being appended) and ai-audit.<n>.jsonl segments
    evidence/           <sha256>.json, content-addressed payloads
    state/              settings, monitor registry and locks, model capabilities,
                        scan plans, submission defaults, analysis runs, workspace edits
    cache/              probe output and tool caches; safe to remove
    migration/          the migration record and files the migration parked

Operator data (runs/, downloads/, structures/, reports/, workbench/, exports/,
servers/, potentials/) stays at the workspace root and is not described here.

Older versions wrote the same files as dot-files at the workspace root. Until the
operator migrates them (vasp_agent.common.workspace_migration), every lookup
resolves one item at a time: the .agent/ copy if it exists, else the legacy copy
if it exists, else the .agent/ location. An existing item is read and updated
where it is, so it never exists twice; only items created from now on land in
.agent/. Two exceptions: the audit log is append-only, always appends under
.agent/audit/ and is read legacy file first; the job monitor registry and its
locks resolve as one set (monitor_paths) so two registries cannot both be live.

File formats do not change; only locations do.
"""
import os
from pathlib import Path
import re

from vasp_agent.common.workspace_paths import workspace_path

AGENT_DIR = '.agent'

# Single files: key -> (location under .agent/, legacy name at the workspace root or None).
FILES = {
    'settings': ('state/agent-settings.json', '.agent-settings.json'),
    'model_capabilities': ('state/model-capabilities.json', '.model-capabilities.json'),
    'terminal_colors': ('state/terminal-colors.json', '.terminal-colors.json'),
    'monitor_events': ('state/monitor-events.jsonl', None),
    'autonomy_policy': ('state/autonomy-policy.json', None),
}

# Directories of independent items: key -> (location under .agent/, legacy directory or None).
STORES = {
    'evidence': ('evidence', '.context-evidence'),
    'scan_plans': ('state/scan-plans', '.scan-plans'),
    'submission_defaults': ('state/submission-defaults', '.submission-defaults'),
    'analysis_runs': ('state/analysis-runs', '.analysis-runs'),
    'workspace_edits': ('state/workspace-edits', '.workspace-edits'),
    'cache': ('cache', '.cache'),
    'contracts': ('state/contracts', None),
    'review_attestations': ('state/review-attestations', None),
}

# The job monitor registry and the two locks that guard it move together.
MONITOR = {
    'registry': ('state/job-monitor.json', '.job-monitor.json'),
    'state_lock': ('state/job-monitor-state.lock', '.job-monitor-state.lock'),
    'worker_lock': ('state/job-monitor-worker.lock', '.job-monitor-worker.lock'),
}
MONITOR_TEMP_PREFIX = 'job-monitor-'
LEGACY_MONITOR_TEMP_PREFIX = '.job-monitor-'

SESSIONS = 'sessions'
SESSION_ARCHIVE = 'sessions/archive'
LEGACY_SESSION_ARCHIVE = '.ai-archive'
SESSION_PREFIX = 'ai-session-'
LEGACY_SESSION_PREFIX = '.ai-session-'
# A session is its history file plus these sidecars; longest first so a name
# is split at the right suffix.
SESSION_SIDECARS = ('.context.parts.json', '.context.errors.jsonl', '.cache-metrics.jsonl',
                    '.context.parts.tmp', '.context.json', '.context.tmp')
SESSION_SUFFIXES = SESSION_SIDECARS + ('.json',)

AUDIT = 'audit'
AUDIT_ACTIVE = 'ai-audit.jsonl'
AUDIT_SEGMENT = re.compile(r'ai-audit\.(\d+)(?:-(\d+))?\.jsonl')
LEGACY_AUDIT = '.ai-audit.jsonl'
AUDIT_SEGMENT_BYTES = 5 * 1024 * 1024

LOCKS = 'state/locks'
MIGRATION = 'migration'


def _root(workspace):
    return Path(workspace).resolve()


def agent_root(workspace):
    return workspace_path(workspace, AGENT_DIR)


def agent_path(workspace, relative):
    """A path under .agent/; refuses anything that would leave the workspace."""
    return workspace_path(workspace, AGENT_DIR + '/' + relative)


def legacy_path(workspace, relative):
    return workspace_path(workspace, relative)


def _first(new, legacy):
    if new.exists() or new.is_symlink():
        return new
    if legacy.exists() or legacy.is_symlink():
        return legacy
    return new


# ---------------------------------------------------------------- single files

def file_path(workspace, key):
    """Where file `key` is read and written: .agent/ copy, else legacy copy (if any), else .agent/."""
    new, legacy = FILES[key]
    if legacy is None:
        return agent_path(workspace, new)
    return _first(agent_path(workspace, new), legacy_path(workspace, legacy))


def new_file_path(workspace, key):
    return agent_path(workspace, FILES[key][0])


# ---------------------------------------------------------------------- stores

def store_dir(workspace, key):
    """The .agent/ directory for store `key` (where new items are created)."""
    return agent_path(workspace, STORES[key][0])


def legacy_store_dir(workspace, key):
    legacy = STORES[key][1]
    return None if legacy is None else legacy_path(workspace, legacy)


def store_item(workspace, key, name):
    """One item of a store: the .agent/ copy, else the legacy copy (if any), else the .agent/ location."""
    if '/' in name or '\\' in name or name in ('', '.', '..'):
        raise ValueError('存储条目名称无效。')
    legacy = legacy_store_dir(workspace, key)
    if legacy is None:
        return store_dir(workspace, key) / name
    return _first(store_dir(workspace, key) / name, legacy / name)


def store_items(workspace, key, pattern='*'):
    """Items of a store in both locations; the .agent/ copy wins when a name exists twice."""
    found = {}
    legacy = legacy_store_dir(workspace, key)
    folders = (store_dir(workspace, key),) if legacy is None else (legacy, store_dir(workspace, key))
    for folder in folders:
        if folder.is_dir():
            for path in folder.glob(pattern):
                if not path.is_symlink() and path.resolve().is_relative_to(_root(workspace)):
                    found[path.name] = path
    return [found[name] for name in sorted(found)]


def store_root(workspace, key):
    """A store used as one unit (it keeps its own lock and journal): whole-directory fallback."""
    new, legacy = store_dir(workspace, key), legacy_store_dir(workspace, key)
    return legacy if legacy is not None and legacy.is_dir() and not new.exists() else new


def cache_dir(workspace, *parts):
    return store_dir(workspace, 'cache').joinpath(*parts)


# --------------------------------------------------------------------- monitor

def monitor_paths(workspace):
    """The registry and its locks, all at .agent/state/ or all at the legacy root.

    Precedence: the .agent/ set unless only the legacy registry exists. A new
    registry is therefore created only where no legacy one exists, and once a
    .agent/ registry exists a legacy registry is ignored (the migration parks it).
    Lock files belong to the registry they guard, so every process that agrees on
    the registry also agrees on the locks.
    """
    new = agent_path(workspace, MONITOR['registry'][0])
    legacy = legacy_path(workspace, MONITOR['registry'][1])
    use_legacy = (legacy.exists() or legacy.is_symlink()) and not (new.exists() or new.is_symlink())
    pick = (lambda key: legacy_path(workspace, MONITOR[key][1])) if use_legacy else (
        lambda key: agent_path(workspace, MONITOR[key][0]))
    folder = legacy.parent if use_legacy else new.parent
    return {'registry': pick('registry'), 'state_lock': pick('state_lock'), 'worker_lock': pick('worker_lock'),
            'temp_dir': folder, 'temp_prefix': LEGACY_MONITOR_TEMP_PREFIX if use_legacy else MONITOR_TEMP_PREFIX,
            'legacy': use_legacy}


# -------------------------------------------------------------------- sessions

def session_dir(workspace, archived=False):
    return agent_path(workspace, SESSION_ARCHIVE if archived else SESSIONS)


def legacy_session_dir(workspace, archived=False):
    return legacy_path(workspace, LEGACY_SESSION_ARCHIVE) if archived else _root(workspace)


def session_file(workspace, session):
    """History file of session `session` (`<name>-<identity>`): .agent/ copy, else legacy, else .agent/."""
    new = workspace_path(session_dir(workspace), SESSION_PREFIX + session + '.json')
    legacy = workspace_path(workspace, LEGACY_SESSION_PREFIX + session + '.json')
    if not new.is_relative_to(session_dir(workspace)):
        raise ValueError('对话名称不合法。')
    return _first(new, legacy)


def split_session_name(filename):
    """(stem, suffix) for a session file name in either layout, or None."""
    for prefix in (SESSION_PREFIX, LEGACY_SESSION_PREFIX):
        if filename.startswith(prefix):
            rest = filename[len(prefix):]
            for suffix in SESSION_SUFFIXES:
                if rest.endswith(suffix) and len(rest) > len(suffix):
                    return rest[:-len(suffix)], suffix
    return None


def session_stem(path):
    """`<name>-<identity>` of a history file, or None for sidecars and other files."""
    parts = split_session_name(Path(path).name)
    return parts[0] if parts and parts[1] == '.json' else None


def session_files(workspace, archived=False):
    """History files of active (or archived) sessions, keyed by stem; .agent/ wins over legacy."""
    found = {}
    folders = (legacy_session_dir(workspace, archived), session_dir(workspace, archived))
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in folder.glob('*.json'):
            stem = session_stem(path)
            if stem and not path.is_symlink() and path.resolve().is_relative_to(_root(workspace)):
                found[stem] = path
    return found


def session_group(history_file):
    """The history file and every sidecar that exists next to it."""
    history_file = Path(history_file)
    stem = session_stem(history_file)
    if stem is None:
        raise ValueError('不是对话记录文件。')
    prefix = history_file.name[:-len(stem + '.json')]
    return [history_file.parent / (prefix + stem + suffix) for suffix in SESSION_SUFFIXES
            if (history_file.parent / (prefix + stem + suffix)).exists()]


def move_session(workspace, history_file, archived, stem=None):
    """Move a session and its sidecars into the active or archive folder of .agent/.

    Nothing is overwritten: if any destination exists the move is refused before
    a file moves. Returns the new history file.
    """
    group = session_group(history_file)
    source_stem = session_stem(history_file)
    stem = stem or source_stem
    prefix = history_file.name[:-len(source_stem + '.json')]
    folder = session_dir(workspace, archived)
    moves = [(path, folder / (SESSION_PREFIX + stem + path.name[len(prefix + source_stem):])) for path in group]
    for _, target in moves:
        if target.exists() or target.is_symlink():
            raise FileExistsError(target.name)
    folder.mkdir(parents=True, exist_ok=True)
    for source, target in moves:
        os.rename(source, target)
    return folder / (SESSION_PREFIX + stem + '.json')


def lock_path(workspace, name):
    return agent_path(workspace, LOCKS + '/' + name + '.lock')


def hold_session(workspace, session):
    """Hold the lock that marks `session` as open in this process; None if another process holds it."""
    from vasp_agent.common.file_lock import FileLock
    path = lock_path(workspace, 'session-' + session)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(path, blocking=False)
    return lock if lock.acquire() else None


def session_in_use(workspace, session):
    """True when another process holds this session's lock."""
    from vasp_agent.common.file_lock import FileLock
    path = lock_path(workspace, 'session-' + session)
    if not path.exists():
        return False
    lock = FileLock(path, blocking=False)
    if lock.acquire():
        lock.release()
        return False
    return True


# ----------------------------------------------------------------------- audit

def audit_file(workspace):
    """The audit file new records are appended to."""
    return agent_path(workspace, AUDIT + '/' + AUDIT_ACTIVE)


def audit_segments(workspace):
    """Every audit file, oldest first: legacy file, numbered segments, then the active file."""
    files = []
    legacy = legacy_path(workspace, LEGACY_AUDIT)
    if legacy.is_file():
        files.append(legacy)
    folder = agent_path(workspace, AUDIT)
    if folder.is_dir():
        numbered = []
        for path in folder.glob('ai-audit.*.jsonl'):
            match = AUDIT_SEGMENT.fullmatch(path.name)
            if match:
                numbered.append(((int(match[1]), int(match[2] or 0)), path))
        files.extend(path for _, path in sorted(numbered))
    active = audit_file(workspace)
    if active.is_file():
        files.append(active)
    return files


def audit_lines(workspace):
    for path in audit_segments(workspace):
        with path.open(encoding='utf-8') as stream:
            yield from stream


def _next_segment(folder):
    numbers = [int(m[1]) for m in (AUDIT_SEGMENT.fullmatch(p.name) for p in folder.glob('ai-audit.*.jsonl')) if m]
    return folder / f'ai-audit.{max(numbers, default=0) + 1:06d}.jsonl'


def rotate_audit(workspace, limit=AUDIT_SEGMENT_BYTES, incoming=0):
    """Close the active audit file as the next numbered segment once `incoming` bytes would pass `limit`.

    The file is renamed whole; nothing is cut or rewritten. On Windows a rename
    fails while another process is appending; the next append retries.
    """
    from vasp_agent.common.file_lock import FileLock
    active = audit_file(workspace)
    lock = FileLock(active.parent / 'rotate.lock')
    lock.acquire()
    try:
        size = active.stat().st_size if active.is_file() else 0
        if size and size + incoming > limit:
            target = _next_segment(active.parent)
            try:
                os.rename(active, target)
                return target
            except OSError:
                return None
    finally:
        lock.release()
    return None


def append_audit(workspace, text, limit=None):
    """Append whole lines to the active audit file, rotating before it would pass `limit`."""
    limit = AUDIT_SEGMENT_BYTES if limit is None else limit
    active = audit_file(workspace)
    active.parent.mkdir(parents=True, exist_ok=True)
    try:
        size = active.stat().st_size
    except FileNotFoundError:
        size = 0
    incoming = len(text.encode('utf-8'))
    if size and size + incoming > limit:
        rotate_audit(workspace, limit, incoming)
    with active.open('a', encoding='utf-8') as stream:
        stream.write(text)


# ------------------------------------------------------------------- migration

def migration_dir(workspace):
    return agent_path(workspace, MIGRATION)


def migration_running(workspace):
    """True while a workspace migration holds its lock."""
    from vasp_agent.common.file_lock import FileLock
    path = lock_path(workspace, 'migration')
    if not path.exists():
        return False
    lock = FileLock(path, blocking=False)
    if lock.acquire():
        lock.release()
        return False
    return True
