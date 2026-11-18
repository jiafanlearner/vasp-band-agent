"""Primitives shared by every remote worker unit: the error type, redaction and file writes.

Part of the stdin worker program (see remote_worker.WORKER_UNITS): standard library only.
"""
import contextlib
import hashlib
import json
import os
import re


class SubmissionError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.error_code = code


def diagnostic_text(value):
    """Bound scheduler diagnostics and redact likely credentials before persistence."""
    if isinstance(value, bytes):
        value = value.decode('utf-8', errors='replace')
    text = str(value or '')
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?(?:-----END [^-]*PRIVATE KEY-----|$)',
                  '[REDACTED PRIVATE KEY]', text, flags=re.S)
    text = re.sub(r'(?i)("(?:password|passwd|api[_-]?key|secret|access[_-]?token|token)"\s*:\s*)"(?:\\.|[^"\\])*"',
                  r'\1"[REDACTED]"', text)
    text = re.sub(r'(?i)(password|passwd|api[_-]?key|token|secret)(\s*[:=]\s*)[^\s,;]+',
                  r'\1\2[REDACTED]', text)
    text = re.sub(r'(?i)(Bearer\s+)\S+', r'\1[REDACTED]', text)
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', text)
    text = ''.join(c for c in text if c.isprintable() or c in '\n\t')
    return text[-12000:]


def write_json(path, data):
    tmp = path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)
    forget_digests()


# The worker's per-action digest memo (see digest_memo); None outside one.
_DIGESTS = None


def file_identity(path):
    """The stat a memoized hash is bound to; the same tuple as the engine's core.file_identity."""
    st = os.stat(path)
    return (os.path.abspath(path), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)


def _sha256_file(path):
    """Full streaming SHA-256, and the identity it is bound to (None if the file moved or changed)."""
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        opened, before = os.fstat(stream.fileno()), file_identity(path)
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
        still, after = os.fstat(stream.fileno()), file_identity(path)
    # The path still names the file that was read, and neither changed during the read.
    # (Both stats are compared: on Windows os.stat and os.fstat fill st_ctime differently.)
    unchanged = (before == after and (opened.st_dev, opened.st_ino) == before[1:3]
                 and all(getattr(opened, k) == getattr(still, k) for k in ('st_size', 'st_mtime_ns', 'st_ctime_ns')))
    return digest.hexdigest(), before if unchanged else None


def file_sha(path):
    """Streaming sha256: a relaxation's vasprun.xml can be far larger than memory should hold."""
    memo = _DIGESTS
    if memo is None:
        return _sha256_file(path)[0]
    value = memo.get(file_identity(path))
    if value is None:
        value, identity = _sha256_file(path)
        if identity is not None:
            memo[identity] = value
    return value


def sha(path):
    return file_sha(path)


@contextlib.contextmanager
def digest_memo():
    """Hash each unchanged file at most once during one worker action.

    Same rules as the engine's core.digest_memo, which the worker enters with this
    same dict once the task's engine is loaded (remote_worker.engine_module), so a
    file the worker checks and the engine checks again is read once. Entered only
    by dispatch, for one action, in one process; write_json clears it.
    """
    global _DIGESTS
    previous = _DIGESTS
    _DIGESTS = {} if previous is None else previous
    try:
        yield _DIGESTS
    finally:
        _DIGESTS = previous


def forget_digests():
    if _DIGESTS is not None:
        _DIGESTS.clear()


# A task's stage order as its own config records it (engine stage_definitions,
# config['stages']), read without loading its engine: the queue, receipt and
# monitor checks run on every task, including those bound to releases older than
# the record. A task without the record runs the legacy four, bands last.
LEGACY_PHASES = ('convergence', 'relaxation', 'scf', 'bands')
PHASE_NAME = re.compile(r'[a-z][a-z0-9_]{0,31}')


def is_phase_name(value):
    return isinstance(value, str) and PHASE_NAME.fullmatch(value) is not None


def config_phases(config):
    stages = config.get('stages') if isinstance(config, dict) else None
    if stages is None:
        return LEGACY_PHASES
    if (not isinstance(stages, list) or not stages
            or any(not isinstance(entry, dict) or not isinstance(entry.get('name'), str)
                   or not is_phase_name(entry['name']) for entry in stages)):
        raise ValueError('任务阶段记录无效。')
    names = tuple(entry['name'] for entry in stages)
    if len(set(names)) != len(names):
        raise ValueError('任务阶段记录重复。')
    return names


def task_phases(folder):
    """Enabled stages of the task in folder, in run order."""
    path = folder / 'config.json'
    if path.is_symlink():
        raise ValueError('计算配置不能是链接。')
    if not path.is_file():
        return LEGACY_PHASES
    if path.stat().st_size > 1_000_000:
        raise ValueError('计算配置过大。')
    return config_phases(json.loads(path.read_text(encoding='utf-8')))
