"""Bounded draft text editing. No subprocesses or formal task mutations."""
import ast
import difflib
import json
import os
import re
from pathlib import Path

from vasp_agent.editing.edit_store import EditStore, digest, guarded, read_bytes
from vasp_agent.common.workspace_paths import local_path
from vasp_agent.workflow.vasp_band_agent import ToolError

MAX_BYTES = 1024 * 1024
PAGE = 12000
EDIT_TOOLS = {'run_local_analysis_script', 'request_analysis_approval_settings', 'get_analysis_runtime', 'validate_analysis_script', 'run_analysis_script', 'get_analysis_run', 'list_workspace_files', 'read_workspace_file', 'search_workspace_text',
              'edit_workspace_file', 'get_workspace_edit', 'revert_workspace_edit', 'update_server_identity'}


def sensitive(text):
    return bool(re.search(r'(?i)PRIVATE KEY|PuTTY-User-Key-File|sk-[A-Za-z0-9_-]{12,}|'
                         r'(?:PAW_PBE|US_PBE).*\n[\s\S]*?(?:TITEL|End of Dataset)|'
                         r'["\']?(?:password|passwd|api[_-]?key|secret|access[_-]?token)["\']?\s*[:=]\s*[^\s,}]+', text))


def safe_value(value):
    """Shared guard for new tool arguments, persistence and presentation."""
    if isinstance(value, str):
        if sensitive(value):
            return '[REDACTED SENSITIVE CONTENT]'
        from vasp_agent.chat.ai_agent import scrub
        return scrub(value)
    if isinstance(value, dict):
        return {k: safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_value(x) for x in value]
    return value


def audit_summary(value):
    value = safe_value(value)
    if isinstance(value, dict):
        return {k: '[正文省略；使用修改 ID 查看]' if k in ('content', 'patch', 'diff', 'text', 'matches')
                else audit_summary(v) for k, v in value.items()}
    if isinstance(value, list):
        return [audit_summary(v) for v in value]
    return value


def decode(data):
    if len(data) > MAX_BYTES:
        raise ToolError('EDIT_TOO_LARGE', '文本超过 1 MiB。')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeError:
        raise ToolError('EDIT_ENCODING', '仅支持 UTF-8 文本。') from None
    if any(ord(c) < 32 and c not in '\r\n\t' for c in text):
        raise ToolError('EDIT_BINARY', '拒绝二进制或含控制字符的文件。')
    if sensitive(text):
        raise ToolError('EDIT_SENSITIVE_CONTENT', '文件或修改包含敏感内容，不读取、展示或备份正文。')
    return text


def validate_text(name, text):
    try:
        if name.lower().endswith('.json'):
            json.loads(text)
            return 'JSON 格式通过；未执行。'
        if name.lower().endswith('.py'):
            ast.parse(text)
            return 'Python 语法通过；未导入或执行。'
        vasp = next((kind for kind in ('POSCAR', 'INCAR', 'KPOINTS')
                     if name.upper() == kind or name.upper().endswith('.' + kind)), None)
        if vasp:
            import warnings
            from pymatgen.io.vasp.inputs import Poscar, Incar, Kpoints
            if not text.strip():
                raise ValueError('Empty VASP draft')
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                parsed = {'POSCAR': Poscar, 'INCAR': Incar, 'KPOINTS': Kpoints}[vasp].from_str(text)
            if caught or vasp == 'INCAR' and not parsed:
                raise ValueError('Parser warning or empty INCAR')
            return vasp + ' 草稿格式检查通过；不代表科学审阅通过。'
    except (ValueError, SyntaxError, KeyError, IndexError, TypeError) as exc:
        line = getattr(exc, 'lineno', None)
        raise ToolError('EDIT_VALIDATION_FAILED', '草稿格式检查失败：' + type(exc).__name__ +
                        (f'，第 {line} 行' if line else '') + '；未写入。') from None
    return '文本已检查；未执行、未验证运行行为。'


def apply_patch_text(original, patch, relative):
    """Strict single-file unified diff, including no-final-newline markers."""
    lines = patch.replace('\r\n', '\n').splitlines()
    if len(lines) < 3 or lines[:2] != ['--- a/' + relative, '+++ b/' + relative]:
        raise ToolError('EDIT_PATCH_INVALID', '补丁必须以 --- a/相对路径 和 +++ b/相对路径 开头，且只能修改一个文件。')
    source = original.replace('\r\n', '\n').splitlines(keepends=True)
    result, cursor, i = [], 0, 2
    while i < len(lines):
        hunk = re.fullmatch(r'@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)', lines[i])
        if not hunk:
            raise ToolError('EDIT_PATCH_INVALID', '缺少合法补丁块。')
        old_start, old_count, new_start, new_count = [int(v) if v is not None else 1 for v in hunk.groups()]
        index = old_start if old_count == 0 else old_start - 1
        if index < cursor or index > len(source):
            raise ToolError('EDIT_PATCH_CONFLICT', '补丁行号不匹配。')
        result.extend(source[cursor:index])
        if (new_start if new_count == 0 else new_start - 1) != len(result):
            raise ToolError('EDIT_PATCH_INVALID', '补丁新行号不匹配。')
        i += 1
        old, new = [], []
        last = None
        while i < len(lines) and not lines[i].startswith('@@ '):
            line = lines[i]
            if line == '\\ No newline at end of file':
                if last is None:
                    raise ToolError('EDIT_PATCH_INVALID', '换行标记位置无效。')
                for part in last:
                    part[-1] = part[-1].removesuffix('\n')
                last = None
            elif line and line[0] in ' +-':
                last = []
                if line[0] in ' -':
                    old.append(line[1:] + '\n'); last.append(old)
                if line[0] in ' +':
                    new.append(line[1:] + '\n'); last.append(new)
            else:
                raise ToolError('EDIT_PATCH_INVALID', '补丁包含不支持的内容。')
            i += 1
        if len(old) != old_count or len(new) != new_count or source[index:index + old_count] != old:
            raise ToolError('EDIT_PATCH_CONFLICT', '补丁原内容不匹配，不进行模糊匹配。')
        result.extend(new)
        cursor = index + old_count
    result.extend(source[cursor:])
    return ''.join(result)


class WorkspaceEditor:
    def __init__(self, workspace):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / 'workbench'
        self.store = EditStore(self.workspace)

    def path(self, value):
        value = local_path(value)
        parts = value.replace('\\', '/').split('/')
        if (Path(value).is_absolute() or re.match(r'^[A-Za-z]:', value) or ':' in value
                or '\x00' in value or any(p == '..' for p in parts)):
            raise ToolError('EDIT_PATH_BLOCKED', '编辑路径必须相对于 workbench/，不得越界。')
        for part in parts:
            lower = part.lower()
            if (part not in ('', '.') and (part.startswith('.') or part.endswith((' ', '.'))
                    or lower in ('potcar', 'auth.json', 'credentials.json', 'authorized_keys', 'known_hosts')
                    or lower.startswith(('id_rsa', 'id_ed25519', 'id_ecdsa', 'potcar.'))
                    or lower.endswith(('.pem', '.key', '.ppk', '.p12', '.pfx'))
                    or re.fullmatch(r'(?i)(?:con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?', part))):
                raise ToolError('EDIT_PATH_BLOCKED', '该文件或目录不允许通用编辑访问。')
        target = guarded(self.root / value)
        if not target.is_relative_to(guarded(self.root)):
            raise ToolError('EDIT_PATH_BLOCKED', '路径超出 workbench/。')
        profiles = guarded(self.workspace / 'servers')
        for profile in profiles.glob('*.json'):
            if profile.name.endswith('.compute.json'):
                continue
            try:
                data = json.loads(guarded(profile).read_text(encoding='utf-8'))
                key = data.get('identity_file')
                if key and os.path.normcase(os.path.abspath(self.workspace / local_path(key))) == os.path.normcase(str(target)):
                    raise ToolError('EDIT_PATH_BLOCKED', '已配置的私钥不能通过通用工具访问。')
            except (json.JSONDecodeError, UnicodeError):
                raise ToolError('EDIT_PATH_BLOCKED', '服务器配置无法校验，暂不访问草稿文件。') from None
        return target

    def read_data(self, path):
        path = self.path(path)
        if not path.is_file():
            raise ToolError('FILE_NOT_FOUND', '草稿文件不存在。')
        if path.stat().st_size > MAX_BYTES:
            raise ToolError('EDIT_TOO_LARGE', '文本超过 1 MiB。')
        data = path.read_bytes()
        decode(data)
        return data

    def files(self, path='.'):
        folder = self.path(path)
        if not folder.exists():
            return []
        if not folder.is_dir():
            raise ToolError('EDIT_PATH_BLOCKED', '列表路径必须为目录。')
        result = []
        for base, dirs, names in os.walk(folder, followlinks=False):
            allowed = []
            for directory in sorted(dirs):
                try:
                    self.path(str((Path(base) / directory).relative_to(self.root)))
                    allowed.append(directory)
                except ToolError:
                    pass
            dirs[:] = allowed
            for name in sorted(names):
                relative = (Path(base) / name).relative_to(self.root).as_posix()
                try:
                    self.path(relative)
                    result.append(relative)
                except ToolError:
                    pass
        return sorted(result)

    def list(self, path='.', offset=0):
        if offset < 0:
            raise ValueError('分页偏移不能为负。')
        files = self.files(path)
        return {'root': 'workbench', 'files': files[offset:offset + 100],
                'next_offset': offset + 100 if len(files) > offset + 100 else None, 'content_is_untrusted': True}

    def read(self, path, start_line=1, offset=0):
        if start_line < 1 or offset < 0:
            raise ValueError('行号或偏移无效。')
        raw = self.read_data(path)
        text = decode(raw)
        text = ''.join(text.splitlines(keepends=True)[start_line - 1:])
        return {'path': path, 'sha256': digest(raw), 'start_line': start_line, 'offset': offset,
                'content': text[offset:offset + PAGE], 'next_offset': offset + PAGE if len(text) > offset + PAGE else None,
                'content_is_untrusted': True}

    def search(self, query, path='.', offset=0):
        if not query or offset < 0:
            raise ValueError('搜索词不能为空，偏移不能为负。')
        results, seen, skipped, size = [], 0, 0, 0
        for name in self.files(path):
            try:
                text = decode(self.read_data(name))
            except ToolError:
                skipped += 1
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if query not in line:
                    continue
                if seen >= offset:
                    excerpt = line[:400]
                    if len(results) == 100 or size + len(excerpt) > PAGE:
                        return {'matches': results, 'next_offset': seen, 'skipped_files': skipped, 'content_is_untrusted': True}
                    results.append({'path': name, 'line': n, 'text': excerpt})
                    size += len(excerpt)
                seen += 1
        return {'matches': results, 'next_offset': None, 'skipped_files': skipped, 'content_is_untrusted': True}

    def checked_record(self, change_id):
        record = self.store.load(change_id)
        if record['kind'] != 'workspace':
            raise ToolError('EDIT_KIND_INVALID', '不是草稿编辑；密钥更新请使用 /server-key --undo。')
        self.path(record['path'])
        for side in ('before', 'after'):
            data = self.store.body(record, side)
            if data is not None:
                decode(data)
        return record

    def result(self, record, offset=0):
        if offset < 0:
            raise ValueError('偏移不能为负。')
        before, after = self.store.body(record, 'before'), self.store.body(record, 'after')
        old = decode(before).splitlines(keepends=True) if before is not None else []
        new = decode(after).splitlines(keepends=True) if after is not None else []
        difference = ''.join(line if line.endswith('\n') else line + '\n\\ No newline at end of file\n'
            for line in difflib.unified_diff(old, new, fromfile='a/' + record['path'], tofile='b/' + record['path']))
        state = self.store.state(record, self.path)
        return {k: v for k, v in {**record, 'current_state': state,
                'edited_version_validation': record['validation'],
                'validation': record['validation'] if state == 'applied' else '当前内容不是本次编辑结果；编辑后版本的验证不适用于当前内容。',
                'diff': difference[offset:offset + PAGE], 'offset': offset,
                'next_offset': offset + PAGE if len(difference) > offset + PAGE else None,
                'added_lines': sum(1 for line in difference.splitlines() if line.startswith('+') and not line.startswith('+++')),
                'removed_lines': sum(1 for line in difference.splitlines() if line.startswith('-') and not line.startswith('---')),
                'content_is_untrusted': True}.items() if k != 'request_signature'}

    def get(self, change_id, offset=0):
        return self.result(self.checked_record(change_id), offset)

    def edit(self, path, operation, expected_sha256, content=None, patch=None, reason='', request_id=None):
        target = self.path(path)
        relative = target.relative_to(self.root).as_posix()
        signature = digest(json.dumps([relative, operation, expected_sha256, content, patch, reason], ensure_ascii=False).encode())
        if any(sensitive(v) for v in (content, patch, reason) if v is not None):
            raise ToolError('EDIT_SENSITIVE_CONTENT', '修改含敏感内容，未保存正文。')
        with self.store.lock():
            previous = self.store.find_request('workspace', request_id, signature)
            if previous:
                self.checked_record(previous['change_id'])
                return self.result(self.store.apply(previous, self.path))
            before = self.read_data(relative) if target.exists() else None
            # A fresh model call can repeat the same completed request after a timeout.
            for file in self.store.root.glob('*.json'):
                record = self.store.load(file.stem)
                if (record['kind'] == 'workspace' and record['request_signature'] == signature
                        and record['status'] == 'applied' and record['after_sha256'] == digest(before)):
                    return {**self.get(file.stem), 'changed': False}
            if operation == 'create':
                if expected_sha256 is not None or patch is not None or content is None:
                    raise ToolError('EDIT_ARGUMENT_INVALID', '新建需 content，expected_sha256 和 patch 必须为 null。')
                after = content.replace('\r\n', '\n').encode('utf-8')
                if before == after:
                    return {'path': relative, 'changed': False, 'sha256': digest(before)}
                if before is not None:
                    raise ToolError('EDIT_CONFLICT', '文件已存在，不覆盖。')
            elif operation == 'patch':
                if before is None or not expected_sha256 or content is not None or patch is None:
                    raise ToolError('EDIT_ARGUMENT_INVALID', '补丁需已有文件、expected_sha256 和 patch；content 必须为 null。')
                if digest(before) != expected_sha256:
                    raise ToolError('EDIT_CONFLICT', '文件哈希已改变，请重新读取。')
                text = apply_patch_text(decode(before), patch, relative)
                if b'\r\n' in before:
                    text = text.replace('\n', '\r\n')
                after = (b'\xef\xbb\xbf' if before.startswith(b'\xef\xbb\xbf') else b'') + text.encode('utf-8')
            else:
                raise ToolError('EDIT_ARGUMENT_INVALID', '仅支持 create 或 patch。')
            text = decode(after)
            validation = validate_text(target.name, text)
            if before == after:
                return {'path': relative, 'changed': False, 'sha256': digest(after), 'validation': validation}
            record = self.store.commit('workspace', relative, before, after,
                {'reason': reason, 'validation': validation}, self.path, request_id, signature)
            return {**self.result(record), 'changed': True}

    def revert(self, change_id):
        with self.store.lock():
            record = self.checked_record(change_id)
            # Validate the current body too: never overwrite a file replaced by a credential.
            if self.path(record['path']).exists():
                self.read_data(record['path'])
            if record['status'] == 'undone' and self.store.state(record, self.path) == 'undone':
                return {**self.result(record), 'changed': False}
            return self.result(self.store.apply(record, self.path, undo=True))

    def changes(self):
        if not self.store.root.exists():
            return []
        records = []
        for path in sorted(self.store.root.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True):
            record = self.store.load(path.stem)
            if record['kind'] == 'workspace':
                records.append({k: record[k] for k in ('change_id', 'path', 'time', 'status')})
            if len(records) == 100:
                break
        return records
