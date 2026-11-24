"""Offline tests for .agents/hooks/context_guard.py and .agents/hooks.json."""
from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOOKS_JSON = ROOT / '.agents/hooks.json'
GUARD_PATH = ROOT / '.agents/hooks/context_guard.py'


def _load_guard():
    spec = importlib.util.spec_from_file_location('context_guard', GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_hooks_json_references_existing_guard_script():
    data = json.loads(HOOKS_JSON.read_text(encoding='utf-8'))
    pre = data['hooks']['PreToolUse']
    assert pre and isinstance(pre, list)
    commands = [h['command'] for entry in pre for h in entry['hooks']]
    assert any('context_guard.py' in cmd for cmd in commands)
    assert GUARD_PATH.is_file()


def test_read_guard_blocks_unbounded_bulky_and_large_files(tmp_path):
    guard = _load_guard()
    big_json = tmp_path / 'huge.json'
    big_json.write_text('{"a": "' + 'x' * 220_000 + '"}', encoding='utf-8')

    allowed, reason = guard.evaluate({'tool_name': 'Read', 'tool_input': {'file_path': str(big_json)}})
    assert not allowed and 'KB' in reason

    allowed, reason = guard.evaluate({'tool_name': 'read_file', 'tool_input': {'Path': 'runs/job1/vasprun.xml'}})
    assert not allowed and ('冷区' in reason or '大日志' in reason)

    allowed, reason = guard.evaluate({'tool_name': 'Read', 'tool_input': {'file_path': '.agent/audit/ai-audit.jsonl'}})
    assert not allowed


def test_read_guard_allows_bounded_or_small_source_reads(tmp_path):
    guard = _load_guard()
    big_json = tmp_path / 'huge.json'
    big_json.write_text('{"a": "' + 'x' * 220_000 + '"}', encoding='utf-8')
    small_py = tmp_path / 'small.py'
    small_py.write_text('print("ok")\n', encoding='utf-8')

    assert guard.evaluate({'tool_name': 'Read', 'tool_input': {'file_path': str(small_py)}}) == (True, None)
    assert guard.evaluate({'tool_name': 'Read', 'tool_input': {'file_path': str(big_json), 'offset': 1, 'limit': 100}}) == (True, None)
    assert guard.evaluate({'tool_name': 'read_file', 'tool_input': {'Path': str(big_json), 'StartLine': 1, 'EndLine': 120}}) == (True, None)


def test_shell_guard_blocks_powershell51_pitfalls_and_raw_log_dumps():
    guard = _load_guard()

    allowed, reason = guard.evaluate(
        {'tool_name': 'run_command', 'tool_input': {'CommandLine': 'git status && git diff'}},
        is_windows=True,
    )
    assert not allowed and '&&' in reason

    allowed, reason = guard.evaluate(
        {'tool_name': 'Bash', 'tool_input': {'command': 'python - <<EOF\nprint(1)\nEOF'}},
        is_windows=True,
    )
    assert not allowed and 'heredoc' in reason

    allowed, reason = guard.evaluate(
        {'tool_name': 'PowerShell', 'tool_input': {'command': 'Get-Content .agent/audit/ai-audit.jsonl'}},
        is_windows=True,
    )
    assert not allowed and 'Tail' in reason


def test_shell_guard_allows_valid_powershell_and_quoted_operators():
    guard = _load_guard()

    assert guard.evaluate(
        {'tool_name': 'run_command', 'tool_input': {'CommandLine': 'git status; git diff'}},
        is_windows=True,
    ) == (True, None)

    assert guard.evaluate(
        {'tool_name': 'run_command', 'tool_input': {'CommandLine': "@'\nprint('a && b')\n'@ | python -"}},
        is_windows=True,
    ) == (True, None)

    assert guard.evaluate(
        {'tool_name': 'run_command', 'tool_input': {'CommandLine': 'Get-Content -Tail 50 app.log'}},
        is_windows=True,
    ) == (True, None)


def test_context_guard_cli_exit_codes():
    blocked = subprocess.run(
        [sys.executable, str(GUARD_PATH)],
        input=json.dumps({'tool_name': 'Read', 'tool_input': {'file_path': 'foo/vasprun.xml'}}),
        capture_output=True,
        text=True,
        encoding='utf-8',
        timeout=15,
    )
    assert blocked.returncode == 2
    assert json.loads(blocked.stdout)['decision'] == 'block'

    allowed = subprocess.run(
        [sys.executable, str(GUARD_PATH)],
        input=json.dumps({'tool_name': 'Read', 'tool_input': {'file_path': 'README.md'}}),
        capture_output=True,
        text=True,
        encoding='utf-8',
        timeout=15,
    )
    assert allowed.returncode == 0
