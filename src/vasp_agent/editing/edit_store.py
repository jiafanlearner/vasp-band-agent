"""Local-only guarded, recoverable single-file changes; never deployed to HPC."""
import contextlib
import hashlib
import json
import os
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from vasp_agent.workflow.vasp_band_agent import ToolError


def digest(data):
    return hashlib.sha256(data).hexdigest() if data is not None else None


def guarded(path):
    """Reject aliases before resolve; also inspect existing ancestors on Windows."""
    path = Path(os.path.abspath(path))
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, 'st_file_attributes', 0) & 0x400:
            raise ToolError('EDIT_PATH_BLOCKED', '不允许链接或 Windows reparse 路径。')
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ToolError('EDIT_PATH_BLOCKED', '仅允许普通文件和目录。')
        if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            raise ToolError('EDIT_PATH_BLOCKED', '不允许硬链接文件。')
    return path


def read_bytes(path):
    path = guarded(path)
    return path.read_bytes() if path.exists() else None


def atomic_write(path, data):
    path = guarded(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    guarded(path)
    fd, temporary = tempfile.mkstemp(prefix='.edit-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            os.chmod(temporary, stat.S_IMODE(path.stat().st_mode))
        guarded(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class EditStore:
    def __init__(self, workspace):
        from vasp_agent.common import agent_paths
        # One unit with its own lock and journal: whole-store fallback, never split.
        self.root = guarded(agent_paths.store_root(workspace, 'workspace_edits'))

    @contextlib.contextmanager
    def lock(self):
        guarded(self.root).mkdir(parents=True, exist_ok=True)
        path = guarded(self.root / '.lock')
        with path.open('a+b') as handle:
            if path.stat().st_size == 0:
                handle.write(b'0')
                handle.flush()
            handle.seek(0)
            try:
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ToolError('EDIT_BUSY', '另一个编辑正在进行，请稍后核查。') from None
            try:
                yield
            finally:
                handle.seek(0)
                if os.name == 'nt':
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def path(self, change_id, suffix='.json'):
        import re
        if not re.fullmatch('[a-f0-9]{32}', change_id):
            raise ToolError('EDIT_ID_INVALID', '修改 ID 无效。')
        return guarded(self.root / (change_id + suffix))

    def load(self, change_id):
        return json.loads(self.path(change_id).read_text(encoding='utf-8'))

    def save(self, record):
        atomic_write(self.path(record['change_id']), json.dumps(record, ensure_ascii=False, indent=2).encode())

    def body(self, record, side):
        expected = record[side + '_sha256']
        if expected is None:
            return None
        data = self.path(record['change_id'], '.' + side).read_bytes()
        if digest(data) != expected:
            raise ToolError('EDIT_RECEIPT_INVALID', '备份哈希不匹配，停止恢复。')
        return data

    def find_request(self, kind, request_id, signature):
        if not request_id:
            return None
        change_id = hashlib.sha256((kind + ':' + request_id).encode()).hexdigest()[:32]
        if not self.path(change_id).exists():
            return None
        record = self.load(change_id)
        if record['kind'] != kind or record['request_signature'] != signature:
            raise ToolError('EDIT_REQUEST_CONFLICT', '相同调用 ID 对应不同修改。')
        return record

    def commit(self, kind, relative, before, after, metadata, resolver, request_id=None, signature=None):
        change_id = hashlib.sha256((kind + ':' + request_id).encode()).hexdigest()[:32] if request_id else uuid4().hex
        record = dict(change_id=change_id, kind=kind, path=relative, before_sha256=digest(before),
                      after_sha256=digest(after), status='prepared', time=datetime.now(timezone.utc).isoformat(),
                      request_signature=signature, **metadata)
        for side, data in [('before', before), ('after', after)]:
            if data is not None:
                atomic_write(self.path(change_id, '.' + side), data)
        self.save(record)
        return self.apply(record, resolver)

    def apply(self, record, resolver, undo=False):
        if record['status'] in ('undone', 'undo_prepared') and not undo:
            raise ToolError('EDIT_ALREADY_REVERTED', '该修改已撤销或正在撤销，不能重放；请查看修改记录。')
        path = resolver(record['path'])
        before, after = self.body(record, 'before'), self.body(record, 'after')
        source, target = (after, before) if undo else (before, after)
        current = read_bytes(path)
        if record['status'] == 'undone' and current != target:
            raise ToolError('EDIT_CONFLICT', '撤销后文件再次变化，不覆盖。')
        # During recovery, the rename may have completed before the receipt write.
        if current == target:
            record['status'] = 'undone' if undo else 'applied'
            self.save(record)
            return {**record, 'already_applied': True}
        if current != source:
            raise ToolError('EDIT_CONFLICT', '文件已改变，停止写入；请重新读取并核对修改。')
        if undo:
            record['status'] = 'undo_prepared'
            self.save(record)
        if read_bytes(resolver(record['path'])) != source:
            raise ToolError('EDIT_CONFLICT', '写入前检测到文件变化。')
        if target is None:
            path.unlink()  # Only a file created by this recorded change.
        else:
            atomic_write(path, target)
        record['status'] = 'undone' if undo else 'applied'
        self.save(record)
        return record

    def state(self, record, resolver):
        current = digest(read_bytes(resolver(record['path'])))
        if record['status'] == 'undone' and current == record['before_sha256']:
            return 'undone'
        if current == record['after_sha256']:
            return 'applied'
        if current == record['before_sha256']:
            return 'not_applied' if record['status'] == 'prepared' else 'restored'
        return 'conflict'
