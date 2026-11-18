"""Import only an explicitly named local POSCAR, without expanding workspace access."""
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from uuid import uuid4
from vasp_agent.common.workspace_paths import local_path


def _regular(path):
    for part in (path, *path.parents):
        if part.is_symlink() or (part.exists() and getattr(part.stat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('结构导入不接受链接或重解析路径。')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError('结构来源必须是普通文件，不能是硬链接。')
    if info.st_size > 5_000_000:
        raise ValueError('结构文件超过5 MB。')
    return info


def import_poscar(workspace, value, user_message):
    from pymatgen.io.vasp.inputs import Poscar
    raw = os.fspath(value).strip().strip('"\'')
    # Exact user spelling (including Git Bash spelling) must be present as a token.
    spellings = {raw, raw.replace('\\', '/')}
    normalized_message = user_message.replace('\\', '/')
    if not any(re.search(r'(?<![\w/.:~-])' + re.escape(v.replace('\\', '/')) +
                         r'(?=$|[\s"\'`，。；：,;!?）)])', normalized_message) for v in spellings):
        raise ValueError('外部导入只接受用户当前消息明确提供的完整文件路径。')
    source = Path(local_path(raw))
    if not source.is_absolute() or str(source).startswith(('\\\\', '//')):
        raise ValueError('请提供本机绝对路径，不接受网络共享路径。')
    if source.name.lower() in ('.env', 'potcar') or source.suffix.lower() in ('.key', '.pem', '.pfx'):
        raise ValueError('不能将凭据或POTCAR作为结构导入。')
    before = _regular(source)
    with source.open('rb') as stream:
        data = stream.read(5_000_001)
    after = _regular(source)
    if len(data) > 5_000_000 or (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('结构文件过大或读取期间改变。')
    try:
        text = data.decode('utf-8-sig')
        if '\x00' in text or re.search(r'PRIVATE KEY|(?i:api[_-]?key|password|passwd)\s*[:=]', text):
            raise ValueError()
        parsed = Poscar.from_str(text)
        structure = parsed.structure
        import numpy as np
        if not len(structure) or not np.isfinite(structure.volume) or structure.volume <= 0 or not np.all(np.isfinite(structure.cart_coords)):
            raise ValueError()
    except Exception:
        raise ValueError('文件不是有效UTF-8 POSCAR，或包含不允许的敏感内容；未复制。') from None
    root = Path(workspace).resolve()
    folder = root / 'structures'
    for part in (folder,):
        if part.is_symlink() or (part.exists() and getattr(part.stat(), 'st_file_attributes', 0) & 0x400):
            raise ValueError('结构导入目录不能是链接或重解析点。')
    folder.mkdir(exist_ok=True)
    digest = hashlib.sha256(data).hexdigest()
    target = folder / (digest[:16] + '-' + uuid4().hex[:8] + '.POSCAR')
    with target.open('xb') as stream:
        stream.write(data)
    receipt = {'source': str(source), 'path': target.relative_to(root).as_posix(),
               'sha256': digest, 'formula': structure.composition.reduced_formula, 'atoms': len(structure),
               'format_validated': True, 'magnetic_state': 'not established by structure file',
               'content_is_untrusted': True, 'prepared': False, 'submitted': False}
    with target.with_suffix('.import.json').open('x', encoding='utf-8') as stream:
        json.dump(receipt, stream, ensure_ascii=False, indent=2)
    return receipt
