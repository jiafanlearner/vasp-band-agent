"""PreToolUse Layer 5 autonomy guard: block protected-path mutations and bare SSH/Slurm bypasses.

Standard library only. Reads a JSON tool-call payload from stdin and supports both:
  - Antigravity camelCase (`{"toolCall": {"name": ..., "args": ...}}`):
      allow -> exit 0 (empty stdout); block -> stdout `{"decision": "deny", "reason": ...}`, exit 0
  - Claude Code / Codex snake_case (`{"tool_name": ..., "tool_input": ...}`):
      allow -> exit 0; block -> stdout `{"decision": "block", "reason": ...}`, exit 2
"""
from __future__ import annotations

import base64
import importlib.util
import json
import os
import posixpath
import re
import sys
from pathlib import Path
from typing import Any


def _load_shell_lex():
    lex_path = Path(__file__).resolve().with_name('_shell_lex.py')
    spec = importlib.util.spec_from_file_location('_vasp_autonomy_shell_lex', lex_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_lex = _load_shell_lex()
FORBIDDEN_REMOTE_CMDS = _lex.FORBIDDEN_REMOTE_CMDS
PREFIX_WRAPPERS = _lex.PREFIX_WRAPPERS

WRITE_TOOLS = frozenset({
    'write', 'write_file', 'write_to_file', 'create_file', 'edit', 'edit_file', 'multi_edit',
    'multiedit', 'replace_file_content', 'multi_replace_file_content', 'notebookedit',
    'notebook_edit', 'apply_patch', 'patch_file', 'delete_file', 'move_file', 'rename_file',
})
SHELL_TOOLS = frozenset({'bash', 'run_command', 'runcommand', 'powershell', 'shell', 'terminal', 'exec_command'})
PATH_ARG_KEYS = (
    'file_path', 'path', 'Path', 'TargetFile', 'AbsolutePath',
    'target_path', 'destination', 'source', 'src', 'dst', 'old_path', 'new_path',
)
PROTECTED_TARGETS: tuple[tuple[str, str], ...] = (
    ('.agent/state/contracts', '合同草案/已签合同/账本目录（须经 vasp_draft_contract 或操作者本地终端 vasp-ctl 操作）'),
    ('.agent/state/review-attestations', '审阅读回执租约与本地背书目录（须经 vasp_get_stage_review / vasp_record_stage_review 操作）'),
    ('.agent/state/autonomy-policy.json', '自治验签策略配置（仅允许操作者本机手工维护）'),
    ('servers', '集群连接与 SSH 凭据目录（受保护操作者数据）'),
    ('runs', 'VASP 计算任务输入与审阅产物目录（受保护不可变任务目录）'),
    ('potentials', 'PBE 赝势库目录（受保护操作者数据）'),
)
ALWAYS_PROTECTED_SUBPATHS = ('.agent/state/contracts', '.agent/state/review-attestations', '.agent/state/autonomy-policy.json')
TOPLEVEL_PROTECTED_DIRS = frozenset({'servers', 'runs', 'potentials'})
SAFE_IMMEDIATE_PARENTS = frozenset({
    'src', 'tests', 'fixtures', 'docs', 'integrations', 'workbench', 'examples', 'tools', '.agents', 'vasp_agent',
})
SUBSHELL_WRAPPERS = frozenset({'bash', 'sh', 'zsh', 'dash', 'cmd', 'powershell', 'pwsh'})
CD_CMDS = frozenset({'cd', 'chdir', 'set-location', 'sl', 'push-location', 'pushd'})
MUTATING_CMDS = frozenset({
    'rm', 'del', 'erase', 'rmdir', 'rd', 'mv', 'move', 'ren', 'rename', 'cp', 'copy', 'xcopy',
    'robocopy', 'touch', 'truncate', 'tee', 'ln', 'mklink', 'chmod', 'chown', 'dd', 'unlink',
    'install', 'rsync', 'patch', 'set-content', 'sc', 'out-file', 'add-content', 'ac',
    'clear-content', 'clc', 'remove-item', 'ri', 'move-item', 'mi', 'copy-item', 'cpi',
    'new-item', 'ni', 'rename-item', 'rni',
})
HUMAN_ONLY_CONTRACT_ACTIONS = frozenset({'approve', 'revoke', 'resume'})

_DRIVE_ABS_RE = re.compile(r'^([a-zA-Z]):/(.*)$')
_DRIVE_REL_RE = re.compile(r'^[a-zA-Z]:([^/].*)$')
_REDIR_OP_RE = re.compile(r'^(?:\d*>>?|\d*>\|?&|&>>?)$')
_PATCH_PATH_RE = re.compile(r'^(?:\*\*\*\s+(?:Update|Add|Delete)\s+File:\s+|\*\*\*\s+Move\s+to:\s+|(?:\+\+\+|---)\s+(?:[ab]/)?)(.+?)\s*$')
_SUBPROC_CALL_RE = re.compile(r'\b(?:subprocess\s*\.|os\.system\s*\(|os\.popen\s*\(|Popen\s*\(|execSync\s*\(|spawnSync\s*\()')
_INLINE_FORBIDDEN_BIN_RE = re.compile(r"""(?i)(?:^|[\s'"(\[,/\\])(ssh|scp|sftp|sbatch|scancel)(?:\.exe)?(?=$|[\s'")\],;])""")
_INLINE_WRITE_RE = re.compile(
    r"""(?:\.write_text\s*\(|\.write_bytes\s*\(|\.unlink\s*\(|\.rmdir\s*\(|shutil\.|os\.remove\s*\(|os\.replace\s*\(|os\.rename\s*\(|json\.dump\s*\(|fs\.writeFile|open\s*\([^)]*?(?:mode\s*=\s*)?['"](?:[wax][bt+]*|r\+[bt]*)['"])"""
)
_INLINE_PROTECTED_PATH_RE = re.compile(
    r"""(?i)(?:\.agent[/\\]state[/\\](?:contracts|review-attestations|autonomy-policy\.json)(?:$|[/\\'"\s,)\]])|(?:^|['"\s(/\\])(?:servers|runs|potentials)[/\\])"""
)
_POSIX_SHELL_C_FLAG_RE = re.compile(r'^-[A-Za-z]*c$')


def _normalize_posix(raw: str) -> tuple[str, bool]:
    s = raw.strip().strip('"\'').replace('\\', '/')
    if not s or s.startswith(('http://', 'https://', 'file://')):
        return '', False
    m = _DRIVE_ABS_RE.match(s)
    if m:
        cleaned = posixpath.normpath('/' + m.group(2)).lstrip('/')
        return (f'{m.group(1).lower()}:/{cleaned}' if cleaned else f'{m.group(1).lower()}:/'), True
    m_rel = _DRIVE_REL_RE.match(s)
    if m_rel:
        s = m_rel.group(1)
    if s.startswith('/'):
        return posixpath.normpath(s), True
    cleaned = posixpath.normpath(s)
    while cleaned.startswith('./'):
        cleaned = cleaned[2:]
    return cleaned, False


def _peel_protected_root(norm_abs: str) -> str:
    low = norm_abs.rstrip('/').lower()
    for sub in ALWAYS_PROTECTED_SUBPATHS:
        idx = low.find('/' + sub)
        if idx > 0:
            return low[:idx]
    for d in TOPLEVEL_PROTECTED_DIRS:
        for marker in (f'/{d}/', f'/{d}'):
            idx = low.find(marker)
            if idx > 0 and (marker.endswith('/') or idx + len(marker) == len(low)):
                return low[:idx]
    return low


def _is_repo_root_dir(raw_item: str) -> bool:
    if os.name != 'nt' and _lex.is_windows_drive_raw(raw_item):
        return False
    try:
        p = Path(raw_item)
        return p.is_dir() and any((p / marker).exists() for marker in ('.git', 'pyproject.toml', 'AGENTS.md'))
    except OSError:
        return False


def _roots_and_cwd(payload: dict[str, Any], tool_input: dict[str, Any], extra_cwd: str | None = None) -> tuple[tuple[str, ...], str]:
    raw_ws = [p for p in (payload.get('workspacePaths') or ()) if isinstance(p, str) and p.strip()]
    roots: list[str] = []
    candidates = raw_ws or ([payload['cwd']] if isinstance(payload.get('cwd'), str) and payload['cwd'].strip() else [])
    for item in candidates:
        norm, is_abs = _normalize_posix(item)
        if not is_abs:
            continue
        keep_exact = bool(raw_ws) or _is_repo_root_dir(item)
        base = norm.rstrip('/').lower() if keep_exact else _peel_protected_root(norm)
        if base and base not in roots:
            roots.append(base)
        if not (os.name != 'nt' and _lex.is_windows_drive_raw(item)):
            try:
                if Path(item).exists():
                    rn, rabs = _normalize_posix(os.path.realpath(item))
                    rb = rn.rstrip('/').lower() if keep_exact else _peel_protected_root(rn)
                    if rabs and rb and rb not in roots:
                        roots.append(rb)
            except OSError:
                pass
    raw_exec = extra_cwd or (tool_input.get('cwd') if isinstance(tool_input.get('cwd'), str) else '') or (
        payload.get('cwd') if isinstance(payload.get('cwd'), str) else ''
    )
    exec_cwd = _normalize_posix(raw_exec)[0].rstrip('/') if raw_exec else ''
    return tuple(roots), exec_cwd


def _match_relative_protected(rel_lower: str) -> tuple[str, str] | None:
    for prefix, desc in PROTECTED_TARGETS:
        if rel_lower == prefix or rel_lower.startswith(prefix + '/'):
            return prefix, desc
    return None


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, 'is_junction', lambda: False)())


def _resolve_symlink_redirect(raw_path: str, exec_cwd: str, roots: tuple[str, ...]) -> str | None:
    if os.name != 'nt' and _lex.is_windows_drive_raw(raw_path):
        return None
    norm, is_abs = _normalize_posix(raw_path)
    if not norm:
        return None
    try:
        cand: Path | None = None
        if is_abs:
            cand = Path(raw_path)
        elif exec_cwd and not (os.name != 'nt' and _lex.is_windows_drive_raw(exec_cwd)) and Path(exec_cwd).exists():
            cand = Path(exec_cwd) / norm
        elif roots and not (os.name != 'nt' and _lex.is_windows_drive_raw(roots[0])) and Path(roots[0]).exists():
            cand = Path(roots[0]) / norm
        else:
            rel_p = Path(norm)
            cur = Path('.')
            for part in rel_p.parts:
                cur = cur / part
                if _is_link(cur):
                    cand = rel_p
                    break
        if cand is None:
            return None
        tail: list[str] = []
        anc = cand
        while not anc.exists() and not _is_link(anc):
            if anc.parent == anc:
                return None
            tail.append(anc.name)
            anc = anc.parent
        real_anc = _normalize_posix(os.path.realpath(str(anc)))[0].rstrip('/')
        abs_anc = _normalize_posix(os.path.abspath(str(anc)))[0].rstrip('/')
        if not real_anc or real_anc.lower() == abs_anc.lower():
            return None
        return '/'.join([real_anc, *reversed(tail)]) if tail else real_anc
    except OSError:
        return None


def _match_protected_path(
    raw_path: str, roots: tuple[str, ...] = (), exec_cwd: str = '', *, check_realpath: bool = True,
) -> tuple[str, str] | None:
    norm, is_abs = _normalize_posix(raw_path)
    if not norm or norm in ('.', '..'):
        return None
    lower = norm.lower()
    if not is_abs:
        hit = _match_relative_protected(lower)
        if hit is not None:
            return hit
        if exec_cwd:
            joined, j_abs = _normalize_posix(f'{exec_cwd}/{norm}')
            if joined:
                hit = _match_protected_path(joined, roots, '', check_realpath=False) if j_abs else _match_relative_protected(joined.lower())
                if hit is not None:
                    return hit
    else:
        matched_root = False
        for root in roots:
            if lower == root or lower.startswith(root + '/'):
                matched_root = True
                rel = lower[len(root) + 1:] if len(lower) > len(root) else ''
                hit = _match_relative_protected(rel)
                if hit is not None:
                    return hit
        if not matched_root:
            for sub in ALWAYS_PROTECTED_SUBPATHS:
                marker = '/' + sub
                if lower.endswith(marker) or (marker + '/') in lower:
                    return next((p, d) for p, d in PROTECTED_TARGETS if p == sub)
            parts = [p for p in _DRIVE_ABS_RE.sub(r'\2', lower).split('/') if p]
            for idx, part in enumerate(parts):
                if part in TOPLEVEL_PROTECTED_DIRS and not (idx >= 1 and parts[idx - 1] in SAFE_IMMEDIATE_PARENTS):
                    return next((p, d) for p, d in PROTECTED_TARGETS if p == part)
    if check_realpath:
        redirected = _resolve_symlink_redirect(raw_path, exec_cwd, roots)
        if redirected:
            return _match_protected_path(redirected, roots, '', check_realpath=False)
    return None


def _extract_write_paths(tool_input: dict[str, Any]) -> list[str]:
    paths = [v for k in PATH_ARG_KEYS if isinstance((v := tool_input.get(k)), str) and v.strip()]
    for lk in ('edits', 'files', 'paths'):
        for entry in (tool_input.get(lk) if isinstance(tool_input.get(lk), list) else ()):
            if isinstance(entry, str) and entry.strip():
                paths.append(entry)
            elif isinstance(entry, dict):
                paths.extend(v for k in PATH_ARG_KEYS if isinstance((v := entry.get(k)), str) and v.strip())
    for pk in ('patch', 'diff', 'input'):
        text = tool_input.get(pk)
        if isinstance(text, str) and ('*** ' in text or '+++ ' in text or '--- ' in text):
            for line in text.splitlines():
                m = _PATCH_PATH_RE.match(line.strip())
                if m:
                    cand = m.group(1).split('\t', 1)[0].strip()
                    if cand and cand != '/dev/null':
                        paths.append(cand)
    return paths


def check_write_tool(tool_input: dict[str, Any], roots: tuple[str, ...] = (), exec_cwd: str = '') -> str | None:
    for raw_path in _extract_write_paths(tool_input):
        hit = _match_protected_path(raw_path, roots, exec_cwd)
        if hit is not None:
            return f'禁止直接写入或修改受保护路径 `{raw_path}`（命中 `{hit[0]}`：{hit[1]}）。'
    return None


def _check_inline_script(script: str) -> str | None:
    if not script:
        return None
    if _SUBPROC_CALL_RE.search(script):
        bm = _INLINE_FORBIDDEN_BIN_RE.search(script)
        if bm:
            return f'禁止通过内联脚本调用 `{bm.group(1)}` 绕过 vasp-mcp/vasp-ctl 门禁。'
    if _INLINE_WRITE_RE.search(script) and _INLINE_PROTECTED_PATH_RE.search(script):
        return '禁止通过内联脚本直接修改受保护合同/背书/凭据/任务/赝势路径。'
    return None


def _check_shell_command(command: str, roots: tuple[str, ...], exec_cwd: str = '', depth: int = 0) -> str | None:
    if depth > 3 or not command.strip():
        return None
    cur_cwd = exec_cwd
    prev_heres: list[str] = []
    for tokens, heres in _lex.tokenize_segments(command):
        if not tokens:
            prev_heres = heres
            continue
        for i, (tok, quoted) in enumerate(tokens):
            if not quoted and _REDIR_OP_RE.match(tok) and i + 1 < len(tokens):
                hit = _match_protected_path(tokens[i + 1][0], roots, cur_cwd)
                if hit is not None:
                    return f'禁止通过 Shell 重定向写入受保护路径 `{tokens[i + 1][0]}`（命中 `{hit[0]}`）。'
        cmd_name, args = _lex.unwrap_command(tokens)
        if not cmd_name:
            prev_heres = heres
            continue
        arg_texts = [t for t, _ in args]
        if cmd_name in CD_CMDS:
            dest = next((t for t in arg_texts if not t.startswith('-')), '')
            if dest:
                dn, d_abs = _normalize_posix(dest)
                if dn:
                    cur_cwd = dn.rstrip('/') if d_abs else (_normalize_posix(f'{cur_cwd}/{dn}')[0].rstrip('/') if cur_cwd else dn.rstrip('/'))
            prev_heres = heres
            continue
        if cmd_name in FORBIDDEN_REMOTE_CMDS:
            return f'禁止在代理 Shell 中直接调用 `{cmd_name}` 绕过审计与合同门禁；请改用 vasp-mcp / vasp-ctl 受控工具。'
        if cmd_name in SUBSHELL_WRAPPERS:
            for i, tok in enumerate(arg_texts):
                tl = tok.lower()
                if tl in ('-encodedcommand', '-enc', '-e') and cmd_name in ('powershell', 'pwsh') and i + 1 < len(arg_texts):
                    try:
                        decoded = base64.b64decode(arg_texts[i + 1]).decode('utf-16-le', errors='ignore')
                        nested = _check_shell_command(decoded, roots, cur_cwd, depth + 1)
                        if nested is not None:
                            return nested
                    except Exception:
                        pass
                elif (
                    tl in ('-c', '/c', '/k', '-command') or (cmd_name not in ('cmd', 'powershell', 'pwsh') and _POSIX_SHELL_C_FLAG_RE.match(tok))
                ) and i + 1 < len(arg_texts):
                    inner = ' '.join(arg_texts[i + 1:]) if cmd_name in ('powershell', 'pwsh') else arg_texts[i + 1]
                    nested = _check_shell_command(inner, roots, cur_cwd, depth + 1)
                    if nested is not None:
                        return nested
            for body in prev_heres + heres:
                nested = _check_shell_command(body, roots, cur_cwd, depth + 1)
                if nested is not None:
                    return nested
        is_vasp_ctl = cmd_name == 'vasp-ctl' or (
            bool(_lex.PYTHON_BIN_RE.match(cmd_name)) and any(
                t == '-mvasp_agent.control.cli' or (t == '-m' and i + 1 < len(arg_texts) and arg_texts[i + 1] == 'vasp_agent.control.cli')
                for i, t in enumerate(arg_texts)
            )
        )
        if is_vasp_ctl:
            for i, t in enumerate(arg_texts):
                if t == 'contract' and i + 1 < len(arg_texts) and arg_texts[i + 1] in HUMAN_ONLY_CONTRACT_ACTIONS:
                    return f'禁止代理通过 Shell 执行 `vasp-ctl contract {arg_texts[i + 1]}`；合同签发、恢复与撤销只能由操作者本人在本地真实终端执行。'
        is_in_place = cmd_name in ('sed', 'perl') and any(
            t == '-i' or t.startswith(('-i', '--in-place')) for t in arg_texts
        )
        if cmd_name in MUTATING_CMDS or is_in_place:
            for tok in arg_texts:
                cand = tok.split('=', 1)[1] if ((tok.startswith('-') or tok.lower().startswith('of=')) and '=' in tok) else tok
                if cand.startswith('-') and ':' in cand:
                    cand = cand.split(':', 1)[1]
                if not cand or cand.startswith('-'):
                    continue
                hit = _match_protected_path(cand, roots, cur_cwd)
                if hit is not None:
                    return f'禁止通过 Shell 命令 `{cmd_name}` 修改受保护路径 `{cand}`（命中 `{hit[0]}`）。'
        reason = _check_inline_script(_lex.extract_inline_script(cmd_name, args, prev_heres + heres))
        if reason is not None:
            return reason
        prev_heres = heres
    for inner in _lex.extract_cmd_substs(command):
        nested = _check_shell_command(inner, roots, cur_cwd, depth + 1)
        if nested is not None:
            return nested
    return None


def check_shell_tool(payload: dict[str, Any], tool_input: dict[str, Any]) -> str | None:
    roots, base_cwd = _roots_and_cwd(payload, tool_input)
    for key in ('command', 'CommandLine', 'cmd'):
        val = tool_input.get(key)
        if isinstance(val, str) and val.strip():
            reason = _check_shell_command(val, roots, base_cwd)
            if reason is not None:
                return reason
    for entry in (tool_input.get('commands') if isinstance(tool_input.get('commands'), list) else ()):
        if isinstance(entry, str) and entry.strip():
            reason = _check_shell_command(entry, roots, base_cwd)
        elif isinstance(entry, dict) and isinstance(entry.get('command'), str):
            _, entry_cwd = _roots_and_cwd(payload, tool_input, extra_cwd=entry.get('cwd'))
            reason = _check_shell_command(entry['command'], roots, entry_cwd)
        else:
            reason = None
        if reason is not None:
            return reason
    return None


def _extract_call(payload: dict[str, Any]) -> tuple[str, dict[str, Any], bool]:
    tc = payload.get('toolCall')
    if isinstance(tc, dict) and 'tool_name' not in payload:
        return str(tc.get('name') or '').strip().lower(), (tc.get('args') if isinstance(tc.get('args'), dict) else {}), True
    args = payload.get('tool_input') or payload.get('input')
    return str(payload.get('tool_name') or payload.get('name') or '').strip().lower(), (args if isinstance(args, dict) else {}), False


def evaluate(payload: dict[str, Any]) -> tuple[bool, str | None]:
    tool_name, tool_input, _ = _extract_call(payload)
    if not tool_name or not tool_input:
        return True, None
    if tool_name in WRITE_TOOLS or tool_name.endswith('apply_edits'):
        roots, exec_cwd = _roots_and_cwd(payload, tool_input)
        reason = check_write_tool(tool_input, roots, exec_cwd)
        return (reason is None), reason
    if tool_name in SHELL_TOOLS or tool_name.endswith('run_checks'):
        reason = check_shell_tool(payload, tool_input)
        return (reason is None), reason
    if tool_name.endswith('ctx_execute') and isinstance(tool_input.get('code'), str):
        lang = str(tool_input.get('language') or '').strip().lower()
        if lang in ('shell', 'bash', 'sh', 'powershell', 'pwsh', 'cmd'):
            roots, exec_cwd = _roots_and_cwd(payload, tool_input)
            reason = _check_shell_command(tool_input['code'], roots, exec_cwd)
        else:
            reason = _check_inline_script(tool_input['code'])
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
    if not isinstance(payload, dict):
        return 0
    _, _, is_agy = _extract_call(payload)
    allowed, reason = evaluate(payload)
    if allowed:
        return 0
    sys.stdout.write(json.dumps({'decision': 'deny' if is_agy else 'block', 'reason': reason}, ensure_ascii=False) + '\n')
    sys.stderr.write(f'[autonomy_guard] {reason}\n')
    return 0 if is_agy else 2


if __name__ == '__main__':
    raise SystemExit(main())
