"""PreToolUse context guard: block raw large-file dumps and PowerShell 5.1 pitfalls.

Standard library only. Reads a JSON tool-call payload from stdin and exits:
  0 -> allow
  2 -> block (reason written to stderr and structured JSON on stdout)
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

READ_TOOLS = frozenset({'read', 'read_file', 'view_file'})
SHELL_TOOLS = frozenset({'bash', 'run_command', 'runcommand', 'powershell'})

LARGE_FILE_BYTES = 200_000
MAX_BOUNDED_LINES = 300

BULKY_EXTENSIONS = frozenset({'.log', '.jsonl', '.xml'})
BULKY_FILENAMES = frozenset({'OUTCAR', 'CHGCAR', 'WAVECAR', 'PROCAR', 'vasprun.xml'})
COLD_PREFIXES = (
    '.agent/audit/',
    '.agent/cache/',
    '.agent/evidence/',
    '.agent/sessions/',
    '.agent/state/',
    'downloads/',
    'potentials/',
    'reports/',
    'runs/',
    'servers/',
    'workbench/',
)

HEREDOC_RE = re.compile(r'(?<!<)<<-?\s*[\'"]?[A-Za-z_][A-Za-z0-9_]*[\'"]?')
RAW_DUMP_CMD_RE = re.compile(
    r'(?i)(?:^|[;\n|])\s*(?:cat|type|gc|get-content)\b([^\n;|]*)'
)
BOUNDED_PS_FLAGS_RE = re.compile(r'(?i)(?:-(?:totalcount|head|tail|first|last)\b|select-object\s+-(?:first|last)\b)')
BULKY_ARG_RE = re.compile(
    r'(?i)(?:\.(?:log|jsonl|xml)\b|\b(?:OUTCAR|CHGCAR|WAVECAR|vasprun\.xml)\b)'
)


def _norm_path(raw: str) -> str:
    norm = raw.replace('\\', '/')
    while norm.startswith('./'):
        norm = norm[2:]
    return norm.lstrip('/')


def _is_bounded_read(tool_input: dict) -> bool:
    limit = tool_input.get('limit')
    if isinstance(limit, int) and 0 < limit <= MAX_BOUNDED_LINES:
        return True
    start = tool_input.get('StartLine', tool_input.get('start_line'))
    end = tool_input.get('EndLine', tool_input.get('end_line'))
    if isinstance(start, int) and isinstance(end, int) and 0 <= (end - start + 1) <= MAX_BOUNDED_LINES:
        return True
    return False


def _file_size_bytes(raw_path: str) -> int | None:
    try:
        path = Path(raw_path)
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return None
    return None


def check_read_tool(tool_input: dict) -> str | None:
    raw_path = (
        tool_input.get('file_path')
        or tool_input.get('path')
        or tool_input.get('Path')
        or tool_input.get('AbsolutePath')
    )
    if not isinstance(raw_path, str) or not raw_path.strip():
        return None
    if _is_bounded_read(tool_input):
        return None

    norm = _norm_path(raw_path)
    lower = norm.lower()
    name = Path(norm).name
    suffix = Path(norm).suffix.lower()

    if any(lower.startswith(prefix) or ('/' + prefix) in lower for prefix in COLD_PREFIXES):
        return (
            f'禁止无界裸读冷区/运行时文件 `{raw_path}`；'
            f'请显式指定 <= {MAX_BOUNDED_LINES} 行范围（StartLine/EndLine 或 offset/limit），或用脚本提取摘要。'
        )

    if name in BULKY_FILENAMES or suffix in BULKY_EXTENSIONS:
        return (
            f'禁止无界裸读大日志/数据文件 `{raw_path}`；'
            f'请显式指定 <= {MAX_BOUNDED_LINES} 行范围，或使用尾部抽样 / Python 结构化解析。'
        )

    size = _file_size_bytes(raw_path)
    if size is not None and size > LARGE_FILE_BYTES:
        return (
            f'文件 `{raw_path}` 体积为 {size // 1024} KB（> {LARGE_FILE_BYTES // 1024} KB），禁止无界裸读；'
            f'请传入 <= {MAX_BOUNDED_LINES} 行的起止行号分段阅读，或用脚本定向提取字段。'
        )
    return None


def _strip_quotes_and_herestrings(command: str) -> str:
    """Blank out PowerShell single/double quoted strings and @'...'@ / @\"...\"@ here-strings."""
    out: list[str] = []
    i = 0
    n = len(command)
    while i < n:
        ch = command[i]
        # PowerShell here-strings: @' ... '@ or @" ... "@
        if ch == '@' and i + 1 < n and command[i + 1] in ("'", '"'):
            quote = command[i + 1]
            end_marker = '\n' + quote + '@'
            end = command.find(end_marker, i + 2)
            if end == -1:
                break
            out.append(' ')
            i = end + len(end_marker)
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(' ')
            i += 1
            while i < n:
                if quote == '"' and command[i] == '`':
                    i += 2
                    continue
                if command[i] == quote:
                    if i + 1 < n and command[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        out.append(ch)
        i += 1
    return ''.join(out)


def check_shell_tool(tool_input: dict, is_windows: bool | None = None) -> str | None:
    command = tool_input.get('command') or tool_input.get('CommandLine')
    if not isinstance(command, str) or not command.strip():
        return None
    if is_windows is None:
        is_windows = os.name == 'nt'

    unquoted = _strip_quotes_and_herestrings(command)

    if is_windows:
        if '&&' in unquoted or '||' in unquoted:
            return (
                '检测到 Windows PowerShell 5.1 不支持的 `&&` 或 `||` 链接符；'
                '请改用 `;` 分隔命令，或用 `if ($?) { ... }` 判断前序状态。'
            )
        if HEREDOC_RE.search(unquoted):
            return (
                '检测到 Bash 风格 heredoc（`<<EOF`），Windows PowerShell 5.1 不支持；'
                "请改用 PowerShell here-string（`@' ... '@ | python -`）。"
            )

    for match in RAW_DUMP_CMD_RE.finditer(command):
        segment = match.group(1)
        if BULKY_ARG_RE.search(segment) and not BOUNDED_PS_FLAGS_RE.search(command):
            return (
                '禁止在 Shell 中无界整文件输出 `.log`/`.jsonl`/`.xml`/`OUTCAR`/`CHGCAR`；'
                '请使用 `Get-Content -Tail 100`、`Select-Object -First/-Last` 或 Python 脚本过滤摘要。'
            )
    return None


def evaluate(payload: dict, is_windows: bool | None = None) -> tuple[bool, str | None]:
    tool_name = str(payload.get('tool_name') or payload.get('name') or '').strip().lower()
    tool_input = payload.get('tool_input') or payload.get('input') or {}
    if not isinstance(tool_input, dict):
        return True, None
    if tool_name in READ_TOOLS:
        reason = check_read_tool(tool_input)
        return (reason is None), reason
    if tool_name in SHELL_TOOLS:
        reason = check_shell_tool(tool_input, is_windows=is_windows)
        return (reason is None), reason
    return True, None


def main() -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(encoding='utf-8', errors='replace')
    raw = sys.stdin.read().strip()
    if not raw:
        return 0
    try:
        payload = json.loads(raw)
    except ValueError:
        return 0
    allowed, reason = evaluate(payload)
    if allowed:
        return 0
    decision = {'decision': 'block', 'reason': reason}
    sys.stdout.write(json.dumps(decision, ensure_ascii=False) + '\n')
    sys.stderr.write(f'[context_guard] {reason}\n')
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
