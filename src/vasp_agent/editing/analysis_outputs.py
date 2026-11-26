"""Host-side validation and atomic publishing for controlled Docker /output exports.

Never touches formal VASP task directories. Every exported file lands exclusively
under workbench/outputs/<run_id>/ after full-batch validation (all-or-nothing),
with per-file SHA-256 recorded in the untrusted analysis run receipt.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from .edit_store import guarded
from .workspace_editor import safe_value
from vasp_agent.workflow.vasp_band_agent import ToolError

ALLOWED_SUFFIXES = frozenset({'.csv', '.json', '.txt', '.md', '.png', '.svg', '.pdf'})
MAX_OUTPUT_FILES = 10
MAX_SCANNED_ENTRIES = 64
MAX_SINGLE_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_DEPTH = 4
MAX_SEGMENT_LEN = 128
MAX_REL_PATH_LEN = 240
RUN_ID_RE = re.compile(r'^[a-f0-9]{32}$')
FORBIDDEN_CHARS = set('<>:"/\\|?*')
WINDOWS_RESERVED = frozenset(
    {'CON', 'PRN', 'AUX', 'NUL'}
    | {f'COM{i}' for i in range(1, 10)}
    | {f'LPT{i}' for i in range(1, 10)}
)


def _is_link_or_reparse(st) -> bool:
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & 0x400)


def _validate_segment(name: str) -> None:
    if not isinstance(name, str) or not name or len(name) > MAX_SEGMENT_LEN:
        raise ToolError('OUTPUT_PATH_BLOCKED', '导出文件名长度无效，已整批拒绝导出。')
    try:
        name.encode('utf-8')
    except UnicodeEncodeError:
        raise ToolError('OUTPUT_PATH_BLOCKED', '导出文件名包含无效 UTF-8 字符，已整批拒绝导出。') from None
    if name in ('.', '..') or name.startswith('.') or name != name.strip() or name.endswith('.'):
        raise ToolError('OUTPUT_PATH_BLOCKED', f'导出路径段 {name!r} 不合法，已整批拒绝导出。')
    if any(ord(ch) < 32 or ch in FORBIDDEN_CHARS for ch in name):
        raise ToolError('OUTPUT_PATH_BLOCKED', f'导出路径段 {name!r} 包含非法字符，已整批拒绝导出。')
    stem = name.split('.', 1)[0].rstrip(' ').upper()
    if stem in WINDOWS_RESERVED:
        raise ToolError('OUTPUT_PATH_BLOCKED', f'导出文件名 {name!r} 为系统保留名，已整批拒绝导出。')


def _read_regular_file(path: Path, rel_posix: str) -> bytes:
    flags = os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ToolError('OUTPUT_UNREADABLE', f'无法安全读取导出文件 {rel_posix}（{type(exc).__name__}），已整批拒绝导出。') from None
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode):
            raise ToolError('OUTPUT_NON_REGULAR', f'文件 {rel_posix} 不是普通文件，已整批拒绝导出。')
        if fst.st_nlink > 1:
            raise ToolError('OUTPUT_HARDLINK_BLOCKED', f'文件 {rel_posix} 为硬链接，已整批拒绝导出。')
        if fst.st_size > MAX_SINGLE_FILE_BYTES:
            raise ToolError('OUTPUT_FILE_TOO_LARGE', f'文件 {rel_posix} 超过单文件上限（2 MiB），已整批拒绝导出。')
        chunks = []
        remaining = MAX_SINGLE_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b''.join(chunks)
    except OSError as exc:
        raise ToolError('OUTPUT_UNREADABLE', f'读取导出文件 {rel_posix} 失败（{type(exc).__name__}），已整批拒绝导出。') from None
    finally:
        os.close(fd)
    if len(data) > MAX_SINGLE_FILE_BYTES:
        raise ToolError('OUTPUT_FILE_TOO_LARGE', f'文件 {rel_posix} 超过单文件上限（2 MiB），已整批拒绝导出。')
    return data


def _scan_staging(staging_root: Path) -> tuple[list[dict], int]:
    """Validate every entry under staging_root without following links. All-or-nothing."""
    try:
        root_st = os.lstat(staging_root)
    except OSError as exc:
        raise ToolError('OUTPUT_UNREADABLE', f'无法检查导出临时目录（{type(exc).__name__}），已拒绝导出。') from None
    if _is_link_or_reparse(root_st) or not stat.S_ISDIR(root_st.st_mode):
        raise ToolError('OUTPUT_PATH_BLOCKED', '导出临时目录不是普通目录，已拒绝导出。')
    root_resolved = staging_root.resolve()
    staged: list[dict] = []
    seen_lower: set[str] = set()
    total_bytes = 0
    scanned_entries = 0

    def walk(directory: Path, rel_parts: tuple[str, ...]) -> None:
        nonlocal total_bytes, scanned_entries
        if len(rel_parts) > MAX_DEPTH:
            raise ToolError('OUTPUT_PATH_BLOCKED', f'导出目录层级超过上限（{MAX_DEPTH} 层），已整批拒绝导出。')
        try:
            with os.scandir(directory) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            raise ToolError('OUTPUT_UNREADABLE', f'无法遍历导出目录（{type(exc).__name__}），已整批拒绝导出。') from None
        for entry in entries:
            scanned_entries += 1
            if scanned_entries > MAX_SCANNED_ENTRIES:
                raise ToolError('OUTPUT_TOO_MANY', f'导出目录条目总数超过上限（{MAX_SCANNED_ENTRIES}），已整批拒绝导出。')
            entry_path = Path(entry.path)
            try:
                st = os.lstat(entry_path)
            except OSError as exc:
                raise ToolError('OUTPUT_UNREADABLE', f'无法检查导出条目（{type(exc).__name__}），已整批拒绝导出。') from None
            if _is_link_or_reparse(st):
                raise ToolError('OUTPUT_LINK_BLOCKED', f'检测到符号链接或重解析点 {entry.name!r}，已整批拒绝导出。')
            _validate_segment(entry.name)
            parts = (*rel_parts, entry.name)
            if len(parts) > MAX_DEPTH:
                raise ToolError('OUTPUT_PATH_BLOCKED', f'导出目录层级超过上限（{MAX_DEPTH} 层），已整批拒绝导出。')
            rel_posix = '/'.join(parts)
            if len(rel_posix) > MAX_REL_PATH_LEN:
                raise ToolError('OUTPUT_PATH_BLOCKED', '导出相对路径过长，已整批拒绝导出。')
            key_lower = rel_posix.lower()
            if key_lower in seen_lower:
                raise ToolError('OUTPUT_NAME_COLLISION', f'检测到大小写冲突的导出路径 {rel_posix}，已整批拒绝导出。')
            seen_lower.add(key_lower)
            if not entry_path.resolve().is_relative_to(root_resolved):
                raise ToolError('OUTPUT_PATH_BLOCKED', f'路径 {rel_posix} 越出导出临时目录，已整批拒绝导出。')
            if stat.S_ISDIR(st.st_mode):
                walk(entry_path, parts)
                continue
            if not stat.S_ISREG(st.st_mode):
                raise ToolError('OUTPUT_NON_REGULAR', f'检测到非普通文件 {rel_posix}，已整批拒绝导出。')
            if st.st_nlink > 1:
                raise ToolError('OUTPUT_HARDLINK_BLOCKED', f'检测到硬链接文件 {rel_posix}，已整批拒绝导出。')
            suffix = Path(entry.name).suffix.lower()
            if suffix not in ALLOWED_SUFFIXES:
                raise ToolError('OUTPUT_SUFFIX_BLOCKED',
                                f'文件 {rel_posix} 后缀不在允许白名单（{", ".join(sorted(ALLOWED_SUFFIXES))}）内，已整批拒绝导出。')
            if len(staged) + 1 > MAX_OUTPUT_FILES:
                raise ToolError('OUTPUT_TOO_MANY', f'导出文件数量超过上限（最多 {MAX_OUTPUT_FILES} 个），已整批拒绝导出。')
            if st.st_size > MAX_SINGLE_FILE_BYTES:
                raise ToolError('OUTPUT_FILE_TOO_LARGE', f'文件 {rel_posix} 超过单文件上限（2 MiB），已整批拒绝导出。')
            if total_bytes + st.st_size > MAX_TOTAL_BYTES:
                raise ToolError('OUTPUT_TOTAL_TOO_LARGE', '导出文件总体积超过上限（8 MiB），已整批拒绝导出。')
            data = _read_regular_file(entry_path, rel_posix)
            total_bytes += len(data)
            if total_bytes > MAX_TOTAL_BYTES:
                raise ToolError('OUTPUT_TOTAL_TOO_LARGE', '导出文件总体积超过上限（8 MiB），已整批拒绝导出。')
            staged.append({
                'name': rel_posix,
                'bytes': len(data),
                'sha256': hashlib.sha256(data).hexdigest(),
                'data': data,
            })

    walk(staging_root, ())
    return staged, total_bytes


def _write_file_synced(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0)
    fd = os.open(path, flags, 0o644)
    try:
        view = memoryview(data)
        written = 0
        while written < len(view):
            written += os.write(fd, view[written:])
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_atomically(workspace: Path, run_id: str, staged: list[dict]) -> Path:
    """Write validated files to a sibling temp dir under workbench/outputs and rename atomically."""
    workspace_root = Path(workspace).resolve()
    workbench_root = guarded(workspace_root / 'workbench')
    outputs_root = guarded(workbench_root / 'outputs')
    final_dir = guarded(outputs_root / run_id)
    if not final_dir.is_relative_to(outputs_root):
        raise ToolError('OUTPUT_PATH_BLOCKED', '目标导出目录越出 workbench/outputs/。')
    if final_dir.exists() or final_dir.is_symlink():
        raise ToolError('OUTPUT_EXISTS', f'目标导出目录 workbench/outputs/{run_id} 已存在，拒绝覆盖。')
    outputs_root.mkdir(parents=True, exist_ok=True)
    guarded(outputs_root)
    temp_dir = guarded(Path(tempfile.mkdtemp(prefix=f'.tmp-{run_id}-', dir=outputs_root)))
    try:
        for item in staged:
            target = guarded(temp_dir / item['name'])
            if not target.is_relative_to(temp_dir):
                raise ToolError('OUTPUT_PATH_BLOCKED', f"目标路径 {item['name']} 越出临时导出目录。")
            target.parent.mkdir(parents=True, exist_ok=True)
            guarded(target.parent)
            _write_file_synced(target, item['data'])
        guarded(final_dir)
        os.rename(temp_dir, final_dir)
    except BaseException:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    return final_dir


@contextlib.contextmanager
def staging_directory(enabled: bool):
    """Yield a host-isolated writable staging dir for container /output (UID 65534 compatible)."""
    if not enabled:
        yield None
        return
    # Parent dir stays mode 0o700 (host-user isolated); inner 'out' dir is writable by container UID 65534.
    # On native Linux, non-root host cleanup skips subdirs owned by UID 65534 (prefer flat /output/<file> outputs).
    parent = Path(tempfile.mkdtemp(prefix='vasp-analysis-out-'))
    if os.name != 'nt':
        parent.chmod(0o700)
    stage = parent / 'out'
    stage.mkdir()
    if os.name != 'nt':
        stage.chmod(0o777)
    try:
        yield stage
    finally:
        shutil.rmtree(parent, ignore_errors=True)


def collect_and_publish(workspace: Path, run_id: str, staging_root: Path, exec_status: str) -> dict:
    """Validate staged /output artifacts and publish them atomically to workbench/outputs/<run_id>/."""
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise ToolError('ANALYSIS_ARGUMENT', '运行ID无效。')
    if exec_status != 'completed':
        return {
            'status': 'skipped',
            'reason': f'容器未正常完成（{safe_value(str(exec_status))}），未导出 /output 文件。',
            'output_dir': None,
            'files': [],
            'file_count': 0,
            'total_bytes': 0,
            'content_is_untrusted': True,
        }
    try:
        staged, total_bytes = _scan_staging(Path(staging_root))
        if not staged:
            return {
                'status': 'empty',
                'output_dir': None,
                'files': [],
                'file_count': 0,
                'total_bytes': 0,
                'content_is_untrusted': True,
            }
        _publish_atomically(Path(workspace), run_id, staged)
    except (ToolError, OSError) as exc:
        code = getattr(exc, 'error_code', 'OUTPUT_PUBLISH_FAILED')
        reason = str(exc) if isinstance(exc, ToolError) else f'导出产物落盘失败（{type(exc).__name__}）。'
        return {
            'status': 'rejected',
            'error_code': code,
            'reason': safe_value(reason),
            'output_dir': None,
            'files': [],
            'file_count': 0,
            'total_bytes': 0,
            'content_is_untrusted': True,
        }
    manifest = [
        {
            'name': item['name'],
            'path': f'workbench/outputs/{run_id}/{item["name"]}',
            'workbench_path': f'outputs/{run_id}/{item["name"]}',
            'bytes': item['bytes'],
            'sha256': item['sha256'],
        }
        for item in staged
    ]
    return {
        'status': 'exported',
        'output_dir': f'workbench/outputs/{run_id}',
        'files': manifest,
        'file_count': len(manifest),
        'total_bytes': total_bytes,
        'content_is_untrusted': True,
    }
