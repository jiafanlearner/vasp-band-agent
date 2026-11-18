"""Local path spelling normalization; remote POSIX paths are never converted."""
import os
import re
from pathlib import Path


def local_path(value, windows=None):
    value = os.fspath(value)
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
        value = value[1:-1]
    if (os.name == 'nt' if windows is None else windows):
        value = re.sub(r'^/([A-Za-z])(?=/|$)', lambda m: m[1].upper() + ':', value)
    return value


def workspace_path(root, value):
    root = Path(root).resolve()
    target = (root / local_path(value)).resolve()
    if not target.is_relative_to(root):
        raise ValueError('路径超出授权工作区。')
    return target


def starts_with_path(message):
    # /help stays a command; /c/... and /work/... are path-led prose.
    first = message.split(maxsplit=1)[0] if message.strip() else ''
    return bool(re.match(r'^/[^/\s]+/', first) or first.startswith('//'))
