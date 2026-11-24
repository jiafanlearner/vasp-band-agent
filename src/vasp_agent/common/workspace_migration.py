"""Move the agent's legacy root dot-files into <workspace>/.agent/.

    python ai_agent.py --workspace . --migrate-workspace            # dry run: lists every move
    python ai_agent.py --workspace . --migrate-workspace apply      # performs exactly those moves
    python ai_agent.py --workspace . --migrate-workspace revert-plan
    python ai_agent.py --workspace . --migrate-workspace revert     # moves them back

Rules:
- Files are moved (renamed), never deleted or rewritten. A legacy file whose
  .agent/ counterpart already exists is not the copy in force (agent_paths reads
  .agent/ first), so it is parked under .agent/migration/parked/ instead.
  Emptied legacy directories are removed.
- Every move is written to .agent/migration/migration-record.jsonl before
  ("planned") and after ("done") it happens, with the source, destination and
  sha256. A rerun reconciles an interrupted move from the record and continues
  with whatever is still at the root, so the migration is resumable and
  idempotent; revert replays the record backwards.
- The migration refuses while another process holds the monitor worker or
  state lock, any conversation lock, or another migration's lock. Agent windows
  started from older versions hold no conversation lock: close them first.
- Operator data (runs/, downloads/, structures/, reports/, workbench/, exports/,
  servers/, potentials/) and anything not listed in agent_paths is never touched.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
from uuid import uuid4

from vasp_agent.common import agent_paths as ap

RECORD = 'migration-record.jsonl'
PARKED = 'parked'


def digest(path):
    h = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def _rel(workspace, path):
    return Path(path).relative_to(workspace).as_posix()


def _files(folder):
    """Regular files below `folder`, links skipped (reported separately)."""
    found, links = [], []
    for root, dirs, names in os.walk(folder, followlinks=False):
        root = Path(root)
        for name in list(dirs):
            if (root / name).is_symlink():
                links.append(root / name)
                dirs.remove(name)
        for name in names:
            path = root / name
            (links if path.is_symlink() else found).append(path)
    return sorted(found), links


def _parked(workspace, source, taken):
    base = ap.migration_dir(workspace) / PARKED / _rel(workspace, source)
    target, n = base, 0
    while target.exists() or target in taken:
        n += 1
        target = base.with_name(f'{base.name}.{n}')
    return target


def _audit_target(workspace, taken):
    folder = ap.agent_path(workspace, ap.AUDIT)
    target, n = folder / 'ai-audit.000000.jsonl', 0
    while target.exists() or target in taken:
        n += 1
        target = folder / f'ai-audit.000000-{n}.jsonl'
    return target


def _classify(workspace):
    """(kind, source, intended destination or None for parking) for every legacy item."""
    items, links = [], []
    monitor = {legacy: new for new, legacy in ap.MONITOR.values()}
    files = {legacy: new for new, legacy in ap.FILES.values()}
    stores = {legacy: key for key, (_, legacy) in ap.STORES.items()}
    for entry in sorted(workspace.iterdir()):
        name = entry.name
        if entry.is_symlink():
            if name.startswith(ap.LEGACY_SESSION_PREFIX) or name in monitor or name in files or name in stores:
                links.append(entry)
            continue
        if entry.is_file() and name.startswith(ap.LEGACY_SESSION_PREFIX):
            split = ap.split_session_name(name)
            target = ap.session_dir(workspace) / (ap.SESSION_PREFIX + split[0] + split[1]) if split else None
            items.append(('session', entry, target))
        elif entry.is_file() and name == ap.LEGACY_AUDIT:
            items.append(('audit', entry, 'audit'))
        elif entry.is_file() and name in files:
            items.append(('state', entry, ap.agent_path(workspace, files[name])))
        elif entry.is_file() and name in monitor:
            items.append(('lock' if name.endswith('.lock') else 'monitor', entry, ap.agent_path(workspace, monitor[name])))
        elif entry.is_file() and (name.startswith(ap.LEGACY_MONITOR_TEMP_PREFIX) and name.endswith('.tmp')
                                  or any(name.startswith(f) and name.endswith('.tmp') for f in
                                         [legacy.removesuffix('.json') for legacy in files])):
            items.append(('leftover', entry, None))
        elif entry.is_dir() and name == ap.LEGACY_SESSION_ARCHIVE:
            found, inner = _files(entry)
            links += inner
            for path in found:
                split = ap.split_session_name(path.name) if path.parent == entry else None
                target = ap.session_dir(workspace, True) / (ap.SESSION_PREFIX + split[0] + split[1]) if split else None
                items.append(('archive', path, target))
        elif entry.is_dir() and name in stores:
            found, inner = _files(entry)
            links += inner
            new = ap.store_dir(workspace, stores[name])
            for path in found:
                items.append((stores[name], path, new / path.relative_to(entry)))
    return items, links


def plan(workspace, check=True):
    """Every move the migration would make now, plus what blocks it."""
    workspace = Path(workspace).resolve(strict=True)
    items, links = _classify(workspace)
    moves, taken = [], set()
    for kind, source, target in items:
        note = None
        if target == 'audit':
            target = _audit_target(workspace, taken)
        elif target is None:
            target, note = _parked(workspace, source, taken), 'unrecognized or temporary file'
        elif target.exists() or target in taken:
            try:
                same = target.is_file() and digest(target) == digest(source)
            except OSError:  # e.g. a lock file another process holds
                same = False
            note = ('identical copy already in .agent/' if same else
                    'the .agent/ copy is the one in force; this older copy is parked')
            target = _parked(workspace, source, taken)
        taken.add(target)
        moves.append({'kind': kind, 'source': source, 'destination': target, 'note': note})
    return {'workspace': workspace, 'moves': moves, 'links': links, 'blockers': blockers(workspace) if check else []}


# ------------------------------------------------------------------- locks

def _lock_files(workspace):
    paths = []
    for new, legacy in (ap.MONITOR['worker_lock'], ap.MONITOR['state_lock']):
        paths += [ap.agent_path(workspace, new), ap.legacy_path(workspace, legacy)]
    folder = ap.agent_path(workspace, ap.LOCKS)
    if folder.is_dir():
        paths += sorted(folder.glob('session-*.lock'))
    return [p for p in paths if p.is_file()]


def _describe(workspace, path):
    if path.name.startswith('session-'):
        return '对话 ' + path.name[len('session-'):-len('.lock')] + ' 正在某个 Agent 窗口中打开'
    if 'worker' in path.name:
        return '另一个 Agent 进程正在运行后台作业监控（' + _rel(workspace, path) + '）'
    return '监控记录正在被写入（' + _rel(workspace, path) + '）'


def _acquire_all(workspace):
    """Hold every lock file that exists; (held, blockers)."""
    from vasp_agent.common.file_lock import FileLock
    held, refused = [], []
    for path in _lock_files(workspace):
        lock = FileLock(path, blocking=False)
        if lock.acquire():
            held.append(lock)
        else:
            refused.append(_describe(workspace, path))
    return held, refused


def blockers(workspace):
    workspace = Path(workspace).resolve()
    if ap.migration_running(workspace):
        return ['另一个迁移正在进行']
    held, refused = _acquire_all(workspace)
    for lock in held:
        lock.release()
    return refused


# ------------------------------------------------------------------ record

def _now():
    return datetime.now(timezone.utc).isoformat()


def _append(record, entry):
    record.parent.mkdir(parents=True, exist_ok=True)
    with record.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({**entry, 'time': _now()}, ensure_ascii=False) + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def read_record(workspace):
    record = ap.migration_dir(workspace) / RECORD
    if not record.is_file():
        return []
    entries = []
    for line in record.read_text(encoding='utf-8').splitlines():
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue  # a line cut by a crash; its move is reconciled from the files
    return entries


def _reconcile(workspace, record, run):
    """Finish the bookkeeping of moves interrupted between rename and 'done'."""
    state = {}
    for entry in read_record(workspace):
        if entry.get('event') in ('planned', 'done', 'recovered'):
            state[(entry['source'], entry['destination'])] = entry
    recovered = 0
    for (source, destination), entry in state.items():
        if entry['event'] != 'planned':
            continue
        src, dst = workspace / source, workspace / destination
        if not src.exists() and dst.is_file() and digest(dst) == entry['sha256']:
            _append(record, {**entry, 'run': run, 'event': 'recovered'})
            recovered += 1
    return recovered


# ------------------------------------------------------------------- apply

def _move(workspace, record, run, move, on_move=None):
    source, target = move['source'], move['destination']
    sha, size = digest(source), source.stat().st_size
    entry = {'run': run, 'kind': move['kind'], 'source': _rel(workspace, source),
             'destination': _rel(workspace, target), 'sha256': sha, 'bytes': size}
    if move['note']:
        entry['note'] = move['note']
    _append(record, {**entry, 'event': 'planned'})
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        raise FileExistsError(_rel(workspace, target))
    os.rename(source, target)
    if on_move:
        on_move(move)
    if digest(target) != sha:
        raise RuntimeError('移动后校验和不一致：' + entry['destination'])
    _append(record, {**entry, 'event': 'done'})


def _remove_empty(workspace, record, run):
    removed = []
    roots = [ap.legacy_path(workspace, legacy) for _, legacy in ap.STORES.values()]
    roots.append(ap.legacy_path(workspace, ap.LEGACY_SESSION_ARCHIVE))
    for root in roots:
        if not root.is_dir() or root.is_symlink():
            continue
        for folder, dirs, files in sorted(os.walk(root), key=lambda row: -len(row[0])):
            if not os.listdir(folder):
                os.rmdir(folder)
                removed.append(_rel(workspace, folder))
                _append(record, {'run': run, 'event': 'rmdir', 'source': removed[-1]})
    return removed


def apply(workspace, on_move=None):
    """Run the migration. Returns a result dict; raises nothing for a refusal."""
    from vasp_agent.common.file_lock import FileLock
    workspace = Path(workspace).resolve(strict=True)
    migration_lock = ap.lock_path(workspace, 'migration')
    migration_lock.parent.mkdir(parents=True, exist_ok=True)
    guard = FileLock(migration_lock, blocking=False)
    if not guard.acquire():
        return {'refused': ['另一个迁移正在进行'], 'moved': 0}
    held = []
    try:
        held, refused = _acquire_all(workspace)
        if refused:
            return {'refused': refused, 'moved': 0}
        record = ap.migration_dir(workspace) / RECORD
        run = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid4().hex[:6]
        recovered = _reconcile(workspace, record, run)
        current = plan(workspace, check=False)
        held_paths = {Path(lock.path) for lock in held}
        moves = [m for m in current['moves'] if m['source'] not in held_paths]
        deferred = [m for m in current['moves'] if m['source'] in held_paths]
        if moves or deferred:
            _append(record, {'run': run, 'event': 'start', 'moves': len(current['moves'])})
        for move in moves:
            _move(workspace, record, run, move, on_move)
        for lock in held:
            lock.release()
        held = []
        skipped = []
        for move in deferred:  # legacy lock files, free only once released
            try:
                _move(workspace, record, run, move, on_move)
            except PermissionError:
                skipped.append(_rel(workspace, move['source']))
        removed = _remove_empty(workspace, record, run)
        if moves or deferred:
            _append(record, {'run': run, 'event': 'finish', 'skipped': skipped})
        return {'refused': [], 'moved': len(moves) + len(deferred) - len(skipped), 'recovered': recovered,
                'skipped': skipped, 'removed_empty_dirs': removed, 'links_left': [_rel(workspace, p) for p in current['links']],
                'record': _rel(workspace, record)}
    finally:
        for lock in held:
            lock.release()
        guard.release()


# ------------------------------------------------------------------ revert

def revert_plan(workspace):
    """Moves recorded as done and not yet reverted, newest first."""
    workspace = Path(workspace).resolve(strict=True)
    last = {}
    for entry in read_record(workspace):
        if entry.get('event') in ('done', 'recovered', 'reverted'):
            last[(entry['source'], entry['destination'])] = entry
    pending = [e for e in last.values() if e['event'] in ('done', 'recovered')]
    pending.sort(key=lambda e: e['time'], reverse=True)
    rows = []
    for entry in pending:
        src, dst = workspace / entry['source'], workspace / entry['destination']
        problem = None
        if src.exists():
            problem = '原位置已有文件'
        elif not dst.is_file():
            problem = '迁移后的文件已不在'
        elif digest(dst) != entry['sha256']:
            problem = '迁移后的文件已被修改（例如 Agent 继续写入）'
        rows.append({'entry': entry, 'source': src, 'destination': dst, 'problem': problem})
    return rows


def revert(workspace):
    from vasp_agent.common.file_lock import FileLock
    workspace = Path(workspace).resolve(strict=True)
    guard = FileLock(ap.lock_path(workspace, 'migration'), blocking=False)
    ap.lock_path(workspace, 'migration').parent.mkdir(parents=True, exist_ok=True)
    if not guard.acquire():
        return {'refused': ['另一个迁移正在进行'], 'reverted': 0}
    held = []
    try:
        held, refused = _acquire_all(workspace)
        for lock in held:
            lock.release()  # monitor lock files may be among the moves; the guard keeps agents out
        held = []
        if refused:
            return {'refused': refused, 'reverted': 0}
        record = ap.migration_dir(workspace) / RECORD
        run = 'revert-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid4().hex[:6]
        done, kept = 0, []
        for row in revert_plan(workspace):
            if row['problem']:
                kept.append((row['entry']['destination'], row['problem']))
                continue
            row['source'].parent.mkdir(parents=True, exist_ok=True)
            os.rename(row['destination'], row['source'])
            _append(record, {**row['entry'], 'run': run, 'event': 'reverted'})
            done += 1
        return {'refused': [], 'reverted': done, 'kept': kept}
    finally:
        guard.release()


# ------------------------------------------------------------------ output

def _summary(moves):
    counts = {}
    for move in moves:
        key = move['kind'] + (' (parked)' if move['note'] else '')
        counts[key] = counts.get(key, 0) + 1
    return counts


def plan_text(workspace, limit=None):
    current = plan(workspace)
    ws = current['workspace']
    lines = [f'工作区：{ws}', f'计划移动 {len(current["moves"])} 个文件到 .agent/（只移动，不删除，不改内容）：']
    for kind, count in sorted(_summary(current['moves']).items()):
        lines.append(f'  {kind}: {count}')
    shown = current['moves'] if limit is None else current['moves'][:limit]
    for move in shown:
        lines.append(f'  {_rel(ws, move["source"])} -> {_rel(ws, move["destination"])}'
                     + (f'  [{move["note"]}]' if move['note'] else ''))
    if limit is not None and len(current['moves']) > limit:
        lines.append(f'  ……另有 {len(current["moves"]) - limit} 项；完整列表请运行下面的预览命令。')
    for link in current['links']:
        lines.append(f'  保留原位（链接，不跟随）：{_rel(ws, link)}')
    if current['blockers']:
        lines.append('现在执行会被拒绝：' + '；'.join(current['blockers']))
    if not current['moves']:
        lines.append('没有需要迁移的旧文件。')
    return '\n'.join(lines)


def commands(workspace):
    ws = Path(workspace).resolve()
    return (f'python ai_agent.py --workspace "{ws}" --migrate-workspace\n'
            f'python ai_agent.py --workspace "{ws}" --migrate-workspace apply')


def chat_preview(workspace):
    return (plan_text(workspace, limit=40) + '\n\n'
            '这是只读预览；当前窗口持有对话锁，迁移须在关闭所有 Agent 窗口后在终端执行：\n' + commands(workspace))


def layout_text(workspace):
    ws = Path(workspace).resolve()
    rows = [('sessions/', ap.session_dir(ws)), ('sessions/archive/', ap.session_dir(ws, True)),
            ('audit/', ap.agent_path(ws, ap.AUDIT)), ('evidence/', ap.store_dir(ws, 'evidence')),
            ('state/', ap.agent_path(ws, 'state')), ('cache/', ap.store_dir(ws, 'cache'))]
    lines = [f'Agent 运行文件目录：{ap.agent_root(ws)}']
    for label, path in rows:
        count = sum(1 for p in path.rglob('*') if p.is_file()) if path.is_dir() else 0
        lines.append(f'  {label:<18} {count} 个文件')
    pending = len(plan(ws, check=False)['moves'])
    lines.append(f'工作区根目录尚有 {pending} 个旧位置文件' + ('（/workspace migrate 预览迁移）。' if pending else '。'))
    return '\n'.join(lines)


def run(workspace, mode='plan'):
    """CLI entry; returns the process exit status."""
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    if mode == 'plan':
        print(plan_text(workspace))
        print('\n预览完成，未移动任何文件。执行迁移：\n' + commands(workspace).splitlines()[1])
        return 0
    if mode == 'apply':
        result = apply(workspace)
        if result['refused']:
            print('迁移被拒绝，未移动任何文件：' + '；'.join(result['refused']))
            return 1
        print(f'已移动 {result["moved"]} 个文件；补记中断的移动 {result["recovered"]} 个；'
              f'记录：{result["record"]}')
        for path in result['skipped']:
            print('  暂未移动（仍被占用，重新运行即可）：' + path)
        for path in result['links_left']:
            print('  保留原位（链接）：' + path)
        return 0
    if mode == 'revert-plan':
        rows = revert_plan(workspace)
        ws = Path(workspace).resolve()
        print(f'可撤销 {sum(1 for r in rows if not r["problem"])} 项：')
        for row in rows:
            print(f'  {_rel(ws, row["destination"])} -> {_rel(ws, row["source"])}'
                  + (f'  [保留：{row["problem"]}]' if row['problem'] else ''))
        return 0
    if mode == 'revert':
        result = revert(workspace)
        if result['refused']:
            print('撤销被拒绝：' + '；'.join(result['refused']))
            return 1
        print(f'已移回 {result["reverted"]} 个文件。')
        for path, why in result['kept']:
            print(f'  保留在 .agent/：{path}（{why}）')
        return 0
    raise ValueError(mode)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--workspace', default='.')
    parser.add_argument('mode', nargs='?', default='plan', choices=['plan', 'apply', 'revert-plan', 'revert'])
    args = parser.parse_args(argv)
    return run(args.workspace, args.mode)


if __name__ == '__main__':
    sys.exit(main())
