"""The .agent/ layout: one module owns the paths, legacy reads, migration, archive."""
import hashlib
import json
from pathlib import Path
import re
from types import SimpleNamespace as NS

import pytest

from vasp_agent.chat.ai_agent import Agent, Toolbox
from vasp_agent.chat.chat_commands import Commands
from vasp_agent.common import agent_paths
from vasp_agent.common import workspace_migration as migration
from vasp_agent.monitoring.job_monitor import FileLock, JobMonitor
from vasp_agent.ui.terminal_ui import TerminalUI

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = '123456abcdef'
FOREIGN = '0000000000aa'
REGISTRY = {'version': 1, 'enabled': True, 'auto': True, 'interval_seconds': 900, 'tasks': []}
EVIDENCE = json.dumps({'ok': True, 'result': 'x' * 100})
EVIDENCE_SHA = hashlib.sha256(EVIDENCE.encode()).hexdigest()
PLAN_ID = 'a' * 32
LEGACY_NAMES = ('.ai-session', '.ai-audit', '.context-evidence', '.agent-settings', '.job-monitor', '.model-capabilities',
                '.scan-plans', '.submission-defaults', '.analysis-runs', '.workspace-edits', '.ai-archive',
                '.terminal-colors')


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding='utf-8')
    return path


def legacy_workspace(root):
    """A workspace as older versions left it, plus operator data that must not move."""
    write(root / f'.ai-session-chat-{IDENTITY}.json', '[{"role": "user", "content": "silicon bands"}]')
    write(root / f'.ai-session-chat-{IDENTITY}.context.json', '{"digest": "x"}')
    write(root / f'.ai-session-chat-{IDENTITY}.context.errors.jsonl', '{"e": 1}\n')
    write(root / f'.ai-session-chat-{IDENTITY}.cache-metrics.jsonl', '{"m": 1}\n')
    write(root / f'.ai-session-old-{FOREIGN}.json', '[{"role": "user", "content": "foreign endpoint task"}]')
    write(root / '.ai-archive' / f'.ai-session-gone-{IDENTITY}.json', '[{"role": "user", "content": "archived talk"}]')
    write(root / '.ai-audit.jsonl', '{"n": 1}\n{"n": 2}\n')
    write(root / '.context-evidence' / (EVIDENCE_SHA + '.json'), EVIDENCE)
    write(root / '.agent-settings.json', '{"max_steps": 12}')
    write(root / '.agent-settings.tmp', '{"max_steps": 11}')
    write(root / '.model-capabilities.json', '{"endpoint": {"summary_reasoning": "unsupported"}}')
    write(root / '.job-monitor.json', json.dumps(REGISTRY))
    write(root / '.job-monitor-state.lock', '0')
    write(root / '.job-monitor-worker.lock', '0')
    write(root / '.scan-plans' / (PLAN_ID + '.json'), '{"id": "plan"}')
    write(root / '.submission-defaults' / 'hpc-scan.json', '{"nodes": 1}')
    write(root / '.analysis-runs' / ('b' * 32 + '.json'), '{"run": 1}')
    write(root / '.workspace-edits' / '.lock', '0')
    write(root / '.workspace-edits' / 'c1.json', '{"change": 1}')
    write(root / '.cache' / 'pytest' / 'v' / 'lastfailed', '{}')
    write(root / 'runs' / 'si' / 'INCAR', 'ENCUT = 520')
    write(root / 'servers' / 'hpc.json', '{"host": "example"}')
    write(root / 'workbench' / 'notes.txt', 'operator notes')
    return root


def snapshot(root, skip=('.agent/migration/migration-record', '.agent/state/locks/')):
    def digest(path):
        try:
            return sha(path)
        except PermissionError:  # a lock file this test holds
            return 'locked'
    return {p.relative_to(root).as_posix(): digest(p) for p in root.rglob('*')
            if p.is_file() and not p.relative_to(root).as_posix().startswith(skip)}


OPERATOR = ('runs/si/INCAR', 'servers/hpc.json', 'workbench/notes.txt')


# ---------------------------------------------------------------- one owner

def test_no_runtime_file_name_is_hard_coded_outside_the_path_module():
    owners = {'agent_paths.py', 'workspace_migration.py'}
    pattern = re.compile(r"""['"/](?:%s)""" % '|'.join(re.escape(n) for n in LEGACY_NAMES)
                         + r"""|['"]\.agent['"/]|['"]\.cache['"/]""")
    offenders = []
    for path in (ROOT / 'src').rglob('*.py'):
        if path.name in owners:
            continue
        for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
            code = line.split('#', 1)[0]
            if pattern.search(code):
                offenders.append(f'{path.relative_to(ROOT)}:{number}: {line.strip()}')
    assert offenders == []


def test_engine_bundle_does_not_depend_on_the_local_layout():
    from vasp_agent.remote.remote_worker import SOURCE_FILES
    assert not any('agent_paths' in name or 'workspace_migration' in name for name in SOURCE_FILES)


def test_new_files_are_created_under_dot_agent(tmp_path):
    tools = Toolbox(tmp_path)
    agent = Agent(None, 'test', tools, f'fresh-{IDENTITY}')
    agent.sync_runtime_state()
    tools.log({'event': 'x'})
    tools.store_evidence({'payload': 1})
    assert agent.file == agent_paths.session_dir(tmp_path) / f'ai-session-fresh-{IDENTITY}.json' and agent.file.is_file()
    assert agent_paths.audit_file(tmp_path).is_file()
    assert len(list(agent_paths.store_dir(tmp_path, 'evidence').glob('*.json'))) == 1
    assert [p.name for p in tmp_path.iterdir()] == ['.agent']


# ---------------------------------------------------------- legacy fallback

def test_reads_fall_back_to_legacy_locations_until_migrated(tmp_path):
    legacy_workspace(tmp_path)
    from vasp_agent.chat import agent_settings
    from vasp_agent.planning import parameter_plans
    from vasp_agent.permissions.resource_menu import preference_path
    tools = Toolbox(tmp_path)
    assert agent_settings.load(tmp_path)[0] == {'max_steps': 12}
    assert parameter_plans.path(tools, PLAN_ID) == tmp_path / '.scan-plans' / (PLAN_ID + '.json')
    assert preference_path(tmp_path, 'hpc', 'convergence') == tmp_path / '.submission-defaults' / 'hpc-scan.json'
    assert tools.read_context_evidence(EVIDENCE_SHA, 0)['content'] == EVIDENCE
    assert JobMonitor(tmp_path).path == tmp_path / '.job-monitor.json'
    agent = Agent(None, 'test', tools, f'chat-{IDENTITY}')
    assert agent.file == tmp_path / f'.ai-session-chat-{IDENTITY}.json'
    assert agent.history[0]['content'] == 'silicon bands'
    assert agent.compactor.path.parent == tmp_path  # the sidecars stay with their history
    # An existing item is updated where it lives; a new one goes to .agent/.
    agent_settings.save(tmp_path, {'max_steps': 13})
    assert json.loads((tmp_path / '.agent-settings.json').read_text()) == {'max_steps': 13}
    assert not agent_paths.new_file_path(tmp_path, 'settings').exists()
    tools.log({'event': 'after'})
    assert [json.loads(line) for line in agent_paths.audit_lines(tmp_path)][:2] == [{'n': 1}, {'n': 2}]
    assert json.loads(list(agent_paths.audit_lines(tmp_path))[-1])['event'] == 'after'
    assert (tmp_path / '.ai-audit.jsonl').read_text() == '{"n": 1}\n{"n": 2}\n'


def test_sessions_list_both_layouts_and_dot_agent_wins(tmp_path):
    legacy_workspace(tmp_path)
    write(agent_paths.session_dir(tmp_path) / f'ai-session-chat-{IDENTITY}.json', '[{"role":"user","content":"newer"}]')
    found = agent_paths.session_files(tmp_path)
    assert set(found) == {f'chat-{IDENTITY}', f'old-{FOREIGN}'}
    assert found[f'chat-{IDENTITY}'].parent == agent_paths.session_dir(tmp_path)
    assert set(agent_paths.session_files(tmp_path, archived=True)) == {f'gone-{IDENTITY}'}


# -------------------------------------------------------------- migration

def test_dry_run_lists_every_move_and_changes_nothing(tmp_path):
    legacy_workspace(tmp_path)
    before = snapshot(tmp_path)
    current = migration.plan(tmp_path)
    sources = {m['source'].relative_to(tmp_path).as_posix() for m in current['moves']}
    assert sources == {name for name in before if not name.startswith(OPERATOR)}
    assert current['blockers'] == []
    text = migration.plan_text(tmp_path)
    assert all(name in text for name in sources)
    assert snapshot(tmp_path) == before
    assert migration.run(tmp_path, 'plan') == 0 and snapshot(tmp_path) == before


def test_migration_moves_exactly_the_plan_and_records_hashes(tmp_path):
    legacy_workspace(tmp_path)
    before = snapshot(tmp_path)
    planned = {m['source'].relative_to(tmp_path).as_posix(): m['destination'].relative_to(tmp_path).as_posix()
               for m in migration.plan(tmp_path)['moves']}
    result = migration.apply(tmp_path)
    assert result['refused'] == [] and result['moved'] == len(planned)
    after = snapshot(tmp_path)
    for source, destination in planned.items():
        assert source not in after and after[destination] == before[source]
    for name in OPERATOR:
        assert after[name] == before[name]
    assert sorted(p.name for p in tmp_path.iterdir()) == ['.agent', 'runs', 'servers', 'workbench']
    done = [e for e in migration.read_record(tmp_path) if e['event'] == 'done']
    assert {(e['source'], e['destination'], e['sha256']) for e in done} == {
        (s, d, before[s]) for s, d in planned.items()}
    assert planned['.ai-audit.jsonl'] == '.agent/audit/ai-audit.000000.jsonl'
    assert planned['.agent-settings.tmp'].startswith('.agent/migration/parked/')
    assert planned[f'.ai-session-chat-{IDENTITY}.context.errors.jsonl'] == \
        f'.agent/sessions/ai-session-chat-{IDENTITY}.context.errors.jsonl'
    assert planned[f'.ai-archive/.ai-session-gone-{IDENTITY}.json'] == \
        f'.agent/sessions/archive/ai-session-gone-{IDENTITY}.json'


def test_migration_is_idempotent(tmp_path):
    legacy_workspace(tmp_path)
    migration.apply(tmp_path)
    once = snapshot(tmp_path)
    record = migration.read_record(tmp_path)
    again = migration.apply(tmp_path)
    assert again['moved'] == 0 and again['recovered'] == 0
    assert snapshot(tmp_path) == once and migration.read_record(tmp_path) == record
    assert migration.plan(tmp_path)['moves'] == []


@pytest.mark.parametrize('crash', ['after_rename', 'before_rename'])
def test_interrupted_migration_resumes(tmp_path, monkeypatch, crash):
    legacy_workspace(tmp_path)
    clean = tmp_path / 'clean'
    clean.mkdir()
    legacy_workspace(clean)
    migration.apply(clean)
    expected = snapshot(clean)
    count = {'n': 0}
    if crash == 'after_rename':
        def hook(move):
            count['n'] += 1
            if count['n'] == 3:
                raise KeyboardInterrupt
        with pytest.raises(KeyboardInterrupt):
            migration.apply(tmp_path, on_move=hook)
    else:
        real = migration.os.rename
        def rename(source, target):
            count['n'] += 1
            if count['n'] == 3:
                raise KeyboardInterrupt
            return real(source, target)
        monkeypatch.setattr(migration.os, 'rename', rename)
        with pytest.raises(KeyboardInterrupt):
            migration.apply(tmp_path)
        monkeypatch.setattr(migration.os, 'rename', real)
    assert not agent_paths.migration_running(tmp_path)
    result = migration.apply(tmp_path)
    assert result['recovered'] == (1 if crash == 'after_rename' else 0)
    final = {k: v for k, v in snapshot(tmp_path).items() if not k.startswith('clean/')}
    assert final == expected
    events = migration.read_record(tmp_path)
    done = {(e['source'], e['destination']) for e in events if e['event'] in ('done', 'recovered')}
    planned = {(e['source'], e['destination']) for e in events if e['event'] == 'planned'}
    assert planned <= done | {(e['source'], e['destination']) for e in events
                              if e['event'] == 'planned' and (tmp_path / e['source']).exists()}
    assert migration.plan(tmp_path)['moves'] == []


@pytest.mark.parametrize('lock', ['legacy_worker', 'new_worker', 'session'])
def test_migration_refuses_while_locked(tmp_path, lock):
    legacy_workspace(tmp_path)
    if lock == 'legacy_worker':
        holder = FileLock(tmp_path / '.job-monitor-worker.lock', blocking=False)
        assert holder.acquire()
    elif lock == 'new_worker':
        path = agent_paths.agent_path(tmp_path, agent_paths.MONITOR['worker_lock'][0])
        path.parent.mkdir(parents=True)
        holder = FileLock(path, blocking=False)
        assert holder.acquire()
    else:
        holder = agent_paths.hold_session(tmp_path, f'chat-{IDENTITY}')
    before = snapshot(tmp_path)
    try:
        assert migration.plan(tmp_path)['blockers']
        result = migration.apply(tmp_path)
        assert result['refused'] and result['moved'] == 0
        assert migration.run(tmp_path, 'apply') == 1
        assert snapshot(tmp_path) == before
    finally:
        holder.release()
    assert migration.apply(tmp_path)['moved'] > 0


def test_agent_startup_is_refused_during_a_migration(tmp_path):
    lock = agent_paths.lock_path(tmp_path, 'migration')
    lock.parent.mkdir(parents=True)
    holder = FileLock(lock, blocking=False)
    assert holder.acquire()
    try:
        assert agent_paths.migration_running(tmp_path)
    finally:
        holder.release()
    assert not agent_paths.migration_running(tmp_path)


def test_revert_restores_the_legacy_layout(tmp_path):
    legacy_workspace(tmp_path)
    before = snapshot(tmp_path)
    migration.apply(tmp_path)
    assert all(row['problem'] is None for row in migration.revert_plan(tmp_path))
    result = migration.revert(tmp_path)
    assert result['refused'] == [] and result['kept'] == []
    assert {k: v for k, v in snapshot(tmp_path).items() if not k.startswith('.agent/')} == before
    assert migration.revert_plan(tmp_path) == []


def test_existing_dot_agent_copy_wins_and_the_legacy_copy_is_parked(tmp_path):
    legacy_workspace(tmp_path)
    write(agent_paths.new_file_path(tmp_path, 'settings'), '{"max_steps": 20}')
    moves = {m['source'].name: m for m in migration.plan(tmp_path)['moves']}
    assert moves['.agent-settings.json']['destination'].is_relative_to(agent_paths.migration_dir(tmp_path))
    migration.apply(tmp_path)
    from vasp_agent.chat import agent_settings
    assert agent_settings.load(tmp_path)[0] == {'max_steps': 20}
    assert (agent_paths.migration_dir(tmp_path) / 'parked' / '.agent-settings.json').read_text() == '{"max_steps": 12}'


def test_read_context_evidence_verifies_hashes_after_the_move(tmp_path):
    legacy_workspace(tmp_path)
    migration.apply(tmp_path)
    tools = Toolbox(tmp_path)
    moved = agent_paths.store_dir(tmp_path, 'evidence') / (EVIDENCE_SHA + '.json')
    assert moved.is_file() and not (tmp_path / '.context-evidence').exists()
    assert tools.read_context_evidence(EVIDENCE_SHA, 0)['content'] == EVIDENCE
    moved.write_text(EVIDENCE + ' ', encoding='utf-8')
    with pytest.raises(ValueError, match='哈希不匹配'):
        tools.read_context_evidence(EVIDENCE_SHA, 0)


# ------------------------------------------------------------ job monitor

def test_monitor_registry_precedence(tmp_path):
    paths = agent_paths.monitor_paths(tmp_path)
    assert not paths['legacy'] and paths['registry'] == agent_paths.agent_path(tmp_path, 'state/job-monitor.json')
    write(tmp_path / '.job-monitor.json', json.dumps(REGISTRY))
    paths = agent_paths.monitor_paths(tmp_path)
    assert paths['legacy'] and paths['registry'] == tmp_path / '.job-monitor.json'
    assert paths['worker_lock'] == tmp_path / '.job-monitor-worker.lock'
    write(agent_paths.agent_path(tmp_path, 'state/job-monitor.json'), json.dumps(REGISTRY))
    paths = agent_paths.monitor_paths(tmp_path)
    assert not paths['legacy'] and paths['state_lock'].parent == agent_paths.agent_path(tmp_path, 'state')


def test_single_monitor_registry_after_migration(tmp_path):
    legacy_workspace(tmp_path)
    monitor = JobMonitor(tmp_path)
    monitor.register('hpc', 'si', 'relaxation', '101')
    legacy = tmp_path / '.job-monitor.json'
    assert [t['job_id'] for t in json.loads(legacy.read_text(encoding='utf-8'))['tasks']] == ['101']
    assert not agent_paths.agent_path(tmp_path, 'state/job-monitor.json').exists()
    migration.apply(tmp_path)
    monitor.register('hpc', 'si', 'scf', '102')  # same object, resolved again after the move
    JobMonitor(tmp_path).register('hpc', 'si', 'bands', '103')
    registries = [p for p in tmp_path.rglob('*job-monitor*.json') if 'parked' not in p.parts]
    assert registries == [agent_paths.agent_path(tmp_path, 'state/job-monitor.json')]
    assert [t['job_id'] for t in JobMonitor(tmp_path).status()['tasks']] == ['101', '102', '103']
    assert not legacy.exists() and not (tmp_path / '.job-monitor-state.lock').exists()


# ------------------------------------------------------------------ audit

def test_audit_rotation_keeps_every_line_in_order(tmp_path):
    write(tmp_path / '.ai-audit.jsonl', ''.join(json.dumps({'n': -i}) + '\n' for i in (2, 1)))
    lines = [json.dumps({'n': i, 'pad': 'x' * (i % 7) * 10}) + '\n' for i in range(300)]
    for line in lines:
        agent_paths.append_audit(tmp_path, line, limit=1000)
    segments = agent_paths.audit_segments(tmp_path)
    assert len(segments) > 5
    for path in segments[1:-1]:
        assert path.stat().st_size <= 1000
    expected = ['{"n": -2}\n', '{"n": -1}\n'] + lines
    assert list(agent_paths.audit_lines(tmp_path)) == expected
    migration.apply(tmp_path)
    assert not (tmp_path / '.ai-audit.jsonl').exists()
    assert list(agent_paths.audit_lines(tmp_path)) == expected
    agent_paths.append_audit(tmp_path, '{"n": "last"}\n', limit=1000)
    assert list(agent_paths.audit_lines(tmp_path)) == expected + ['{"n": "last"}\n']


def test_one_oversized_line_is_never_split(tmp_path):
    big = json.dumps({'blob': 'y' * 5000}) + '\n'
    for line in ('{"a": 1}\n', big, '{"b": 2}\n'):
        agent_paths.append_audit(tmp_path, line, limit=1000)
    assert list(agent_paths.audit_lines(tmp_path)) == ['{"a": 1}\n', big, '{"b": 2}\n']


# ---------------------------------------------------------------- sessions

def commands(tmp_path, answers=()):
    tools = Toolbox(tmp_path)
    config = NS(identity=lambda: IDENTITY, public=lambda: {'api_key': '[configured]'})
    responses = iter(answers)
    output = []
    def factory(name):
        return Agent(None, 'test', tools, f'{name}-{config.identity()}')
    c = Commands(factory('default'), factory, config, 'default', TerminalUI('never'),
                 reader=lambda _: next(responses), output=output.append)
    return c, output


def test_sessions_archive_and_restore_move_the_whole_group(tmp_path):
    legacy_workspace(tmp_path)
    c, output = commands(tmp_path)
    group = {p.name for p in tmp_path.glob(f'.ai-session-chat-{IDENTITY}*')}
    assert len(group) == 4
    c.handle('/sessions archive chat')
    archived = agent_paths.session_dir(tmp_path, archived=True)
    assert {p.name for p in archived.glob(f'ai-session-chat-{IDENTITY}*')} == {n[1:] for n in group}
    assert not list(tmp_path.glob(f'.ai-session-chat-{IDENTITY}*'))
    assert 'chat' not in [e[0] for e in c.entries()]
    c.handle('/sessions archived')
    assert any('chat' in line for line in output) and any('gone' in line for line in output)
    c.handle('/sessions restore chat')
    assert {p.name for p in agent_paths.session_dir(tmp_path).glob(f'ai-session-chat-{IDENTITY}*')} == \
        {n[1:] for n in group}
    c.handle('/resume chat')
    assert c.name == 'chat' and c.agent.history[0]['content'] == 'silicon bands'
    c.handle('/sessions restore gone')  # legacy .ai-archive is read too
    assert 'gone' in [e[0] for e in c.entries()]


def test_archive_refuses_open_and_current_sessions_and_never_overwrites(tmp_path):
    legacy_workspace(tmp_path)
    c, output = commands(tmp_path)
    holder = agent_paths.hold_session(tmp_path, f'chat-{IDENTITY}')
    try:
        c.handle('/sessions archive chat default')
    finally:
        holder.release()
    assert any('另一个 Agent 窗口' in line for line in output)
    assert any('/archive chat' in line for line in output)
    assert agent_paths.session_files(tmp_path, archived=True).keys() == {f'gone-{IDENTITY}'}
    write(agent_paths.session_dir(tmp_path, True) / f'ai-session-chat-{IDENTITY}.json', '["older archive"]')
    c.handle('/sessions archive chat')
    archived = agent_paths.session_files(tmp_path, archived=True)
    assert len([s for s in archived if s.startswith('chat-')]) == 2
    assert (agent_paths.session_dir(tmp_path, True) / f'ai-session-chat-{IDENTITY}.json').read_text() == '["older archive"]'
    write(agent_paths.session_dir(tmp_path) / f'ai-session-chat-{IDENTITY}.json', '["live"]')
    c.handle('/sessions restore chat')
    assert any('未覆盖' in line for line in output)


def test_archive_older_than_needs_confirmation(tmp_path):
    import os
    import time
    legacy_workspace(tmp_path)
    old = tmp_path / f'.ai-session-chat-{IDENTITY}.json'
    os.utime(old, (time.time() - 40 * 86400,) * 2)
    c, output = commands(tmp_path, ['q'])
    c.handle('/sessions archive older 30')
    assert old.exists()
    c, output = commands(tmp_path, ['归档'])
    c.handle('/sessions archive older 30')
    assert not old.exists() and f'chat-{IDENTITY}' in agent_paths.session_files(tmp_path, archived=True)
    assert f'old-{FOREIGN}' in agent_paths.session_files(tmp_path)  # recent, not archived


def test_cross_endpoint_transfer_after_archive_and_restore(tmp_path):
    legacy_workspace(tmp_path)
    c, output = commands(tmp_path, ['继续'])
    c.handle(f'/sessions archive old@{FOREIGN}')
    assert f'old-{FOREIGN}' in agent_paths.session_files(tmp_path, archived=True)
    c.handle(f'/resume old@{FOREIGN}')
    assert c.name == 'default'  # archived conversations are not resumed in place
    c.handle(f'/sessions restore old@{FOREIGN}')
    source = agent_paths.session_files(tmp_path)[f'old-{FOREIGN}']
    original = source.read_bytes()
    c.handle(f'/resume old@{FOREIGN}')
    assert c.name == 'old' and 'foreign endpoint task' in c.agent.file.read_text(encoding='utf-8')
    assert c.agent.file.parent == agent_paths.session_dir(tmp_path)
    assert source.read_bytes() == original


def test_import_session_reads_archived_sessions_in_both_layouts(tmp_path):
    legacy_workspace(tmp_path)
    write(tmp_path / '.ai-archive' / f'.ai-session-older-{FOREIGN}.json', '[{"role":"user","content":"legacy archive"}]')
    c, output = commands(tmp_path)
    c.handle(f'/sessions archive old@{FOREIGN}')
    c.reader = iter(['q']).__next__
    c.handle('/import-session')
    listed = [line for line in output if '来源标识' in line]
    assert any(line.split('.')[1].strip().startswith('old') and '已归档' in line for line in listed)
    assert any('older' in line and '已归档' in line for line in listed)
