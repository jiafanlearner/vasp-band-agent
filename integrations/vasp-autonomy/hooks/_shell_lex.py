"""Pure-stdlib shell lexer, heredoc/here-string extractor, and command unwrapper for autonomy_guard."""
from __future__ import annotations

import re

FORBIDDEN_REMOTE_CMDS = frozenset({'ssh', 'scp', 'sftp', 'sbatch', 'scancel'})
PREFIX_WRAPPERS = frozenset({
    'env', 'sudo', 'nohup', 'command', 'exec', 'call', 'time', 'nice', 'timeout', 'start-process', 'saps',
})
WRAPPER_VALUE_FLAGS = frozenset({'-u', '-g', '-c', '-k', '-s', '-n', '-h', '-filepath', '-workingdirectory', '-windowstyle'})

PYTHON_BIN_RE = re.compile(r'^(?:python(?:\d+(?:\.\d+)*)?|py)$')
SCRIPT_BIN_RE = re.compile(r'^(?:python(?:\d+(?:\.\d+)*)?|py|node|perl|ruby)$')
_ENV_ASSIGN_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=.*$')
_HEREDOC_RE = re.compile(r'^<<(-?)\s*(?:([\'"])([A-Za-z_][A-Za-z0-9_]*)\2|([A-Za-z_][A-Za-z0-9_]*))')
_WIN_DRIVE_RAW_RE = re.compile(r'^[a-zA-Z]:')


def is_windows_drive_raw(raw: str) -> bool:
    return bool(_WIN_DRIVE_RAW_RE.match(raw.strip().strip('"\'')))


def tokenize_segments(command: str) -> list[tuple[list[tuple[str, bool]], list[str]]]:
    """Split shell command into segments of ([(token_text, was_quoted)], [heredoc_or_herestring_bodies])."""
    segments: list[tuple[list[tuple[str, bool]], list[str]]] = []
    cur_tokens: list[tuple[str, bool]] = []
    cur_heres: list[str] = []
    pending_heredocs: list[tuple[str, bool]] = []
    buf: list[str] = []
    buf_quoted = False

    def flush_tok() -> None:
        nonlocal buf_quoted
        if buf or buf_quoted:
            cur_tokens.append((''.join(buf), buf_quoted))
            buf.clear()
            buf_quoted = False

    def flush_seg() -> None:
        flush_tok()
        if cur_tokens or cur_heres:
            segments.append((list(cur_tokens), list(cur_heres)))
            cur_tokens.clear()
            cur_heres.clear()

    i, n = 0, len(command)
    while i < n:
        ch = command[i]
        if ch == '@' and i + 1 < n and command[i + 1] in ("'", '"'):
            q = command[i + 1]
            end = command.find('\n' + q + '@', i + 2)
            cur_heres.append(command[i + 2:] if end == -1 else command[i + 2:end])
            i = n if end == -1 else end + 3
            continue
        if ch == '<' and i + 1 < n and command[i + 1] == '<':
            hm = _HEREDOC_RE.match(command[i:])
            if hm:
                pending_heredocs.append((hm.group(3) or hm.group(4), bool(hm.group(1))))
                i += hm.end()
                continue
        if ch in ("'", '"'):
            q = ch
            buf_quoted = True
            i += 1
            while i < n:
                if q == '"' and command[i] == '\\' and i + 1 < n and command[i + 1] in ('\\', '"', '$', '`'):
                    buf.append(command[i + 1])
                    i += 2
                    continue
                if q == '"' and command[i] == '`' and i + 1 < n and command[i + 1] in ('"', '`', '$'):
                    buf.append(command[i + 1])
                    i += 2
                    continue
                if command[i] == q:
                    if i + 1 < n and command[i + 1] == q:
                        buf.append(q)
                        i += 2
                        continue
                    i += 1
                    break
                buf.append(command[i])
                i += 1
            continue
        if ch == '\n':
            if pending_heredocs:
                pos = i + 1
                for delim, strip_tabs in pending_heredocs:
                    body_lines: list[str] = []
                    while pos < n:
                        nl = command.find('\n', pos)
                        line = command[pos:] if nl == -1 else command[pos:nl]
                        pos = n if nl == -1 else nl + 1
                        cmp_line = line.lstrip('\t') if strip_tabs else line
                        if cmp_line.rstrip('\r') == delim:
                            break
                        body_lines.append(line)
                    cur_heres.append('\n'.join(body_lines))
                pending_heredocs.clear()
                flush_seg()
                i = pos
                continue
            flush_seg()
            i += 1
            continue
        if ch in (';', '\r', '(', ')'):
            flush_seg()
            i += 1
            continue
        if ch == '|':
            flush_seg()
            i += 2 if (i + 1 < n and command[i + 1] == '|') else 1
            continue
        if ch == '&' and i + 1 < n and command[i + 1] == '&':
            flush_seg()
            i += 2
            continue
        if ch == '>':
            is_fd = (not buf_quoted) and len(buf) == 1 and buf[0].isdigit()
            if not is_fd:
                flush_tok()
            if i + 1 < n and command[i + 1] == '&':
                buf.append('>&')
                i += 2
                continue
            op = (buf.pop() if is_fd else '') + '>'
            if i + 1 < n and command[i + 1] in ('>', '|'):
                op += command[i + 1]
                i += 2
            else:
                i += 1
            cur_tokens.append((op, False))
            continue
        if ch in (' ', '\t'):
            flush_tok()
            i += 1
            continue
        buf.append(ch)
        i += 1
    flush_seg()
    return segments


def cmd_basename(token: str) -> str:
    cleaned = token.replace('`', '')
    if '/' not in cleaned and not is_windows_drive_raw(cleaned) and '\\' in cleaned:
        de_esc = cleaned.replace('\\', '').lower()
        for ext in ('.exe', '.cmd', '.bat'):
            de_esc = de_esc[:-len(ext)] if de_esc.endswith(ext) else de_esc
        if de_esc in FORBIDDEN_REMOTE_CMDS:
            return de_esc
    base = cleaned.replace('\\', '/').rsplit('/', 1)[-1].lower()
    for ext in ('.exe', '.cmd', '.bat'):
        if base.endswith(ext):
            base = base[:-len(ext)]
    return base


def unwrap_command(tokens: list[tuple[str, bool]]) -> tuple[str, list[tuple[str, bool]]]:
    idx = 0
    while idx < len(tokens):
        text, quoted = tokens[idx]
        if not quoted and (text in ('&', '.') or _ENV_ASSIGN_RE.match(text)):
            idx += 1
            continue
        name = cmd_basename(text)
        if name in PREFIX_WRAPPERS:
            idx += 1
            while idx < len(tokens) and tokens[idx][0].startswith('-'):
                flag = tokens[idx][0].lower()
                idx += 1
                if flag.startswith('-filepath:') or flag.startswith('-workingdirectory:'):
                    if flag.startswith('-filepath:'):
                        return cmd_basename(tokens[idx - 1][0].split(':', 1)[1]), tokens[idx:]
                    continue
                if flag in WRAPPER_VALUE_FLAGS and idx < len(tokens):
                    if flag == '-filepath':
                        return cmd_basename(tokens[idx][0]), tokens[idx + 1:]
                    idx += 1
            if name == 'timeout' and idx < len(tokens) and re.match(r'^\d+(?:\.\d+)?[smhd]?$', tokens[idx][0]):
                idx += 1
            continue
        return name, tokens[idx + 1:]
    return '', []


def extract_inline_script(cmd_name: str, args: list[tuple[str, bool]], piped_bodies: list[str]) -> str:
    if not SCRIPT_BIN_RE.match(cmd_name):
        return ''
    is_py = bool(PYTHON_BIN_RE.match(cmd_name))
    pieces: list[str] = []
    for i, (tok, _q) in enumerate(args):
        if is_py and tok == '-c' and i + 1 < len(args):
            pieces.append(args[i + 1][0])
        elif is_py and tok.startswith('-c') and len(tok) > 2:
            pieces.append(tok[2:])
        elif not is_py and tok in ('-e', '-E') and i + 1 < len(args):
            pieces.append(args[i + 1][0])
        elif tok == '-' and piped_bodies:
            pieces.extend(piped_bodies)
    if not pieces and piped_bodies and not any(t == '-m' or t.startswith('-m') for t, _ in args):
        pieces.extend(piped_bodies)
    return '\n'.join(pieces)


def extract_cmd_substs(command: str) -> list[str]:
    """Extract $(...) (including nested parens) and `...` outside single-quoted literals."""
    out: list[str] = []
    i, n = 0, len(command)
    in_double = False
    while i < n:
        ch = command[i]
        if not in_double and ch == '@' and i + 1 < n and command[i + 1] == "'":
            end = command.find("\n'@", i + 2)
            i = n if end == -1 else end + 3
            continue
        if not in_double and ch == "'":
            i += 1
            while i < n:
                if command[i] == "'":
                    if i + 1 < n and command[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if ch == '"':
            in_double = not in_double
            i += 1
            continue
        if in_double and ch == '\\' and i + 1 < n:
            i += 2
            continue
        if ch == '$' and i + 1 < n and command[i + 1] == '(':
            start, depth, j = i + 2, 1, i + 2
            while j < n and depth > 0:
                if command[j] == '(':
                    depth += 1
                elif command[j] == ')':
                    depth -= 1
                j += 1
            if depth == 0:
                out.append(command[start:j - 1])
                i = j
                continue
        if ch == '`':
            end = command.find('`', i + 1)
            if end != -1:
                out.append(command[i + 1:end])
                i = end + 1
                continue
        i += 1
    return out
