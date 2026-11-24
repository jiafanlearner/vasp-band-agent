"""Cross-platform advisory file lock shared by workspace paths, migration and monitoring."""
from __future__ import annotations

import os


class FileLockError(ValueError):
    """Base error raised when a lock file is invalid or cannot be acquired."""


class FileLockContentionError(FileLockError):
    """Raised when a blocking lock cannot be acquired due to transient contention."""


class FileLock:
    """OS locks release on process exit; lock files themselves are never deleted."""

    def __init__(self, path, blocking=True, error_type=FileLockError, contention_error_type=None):
        self.path = path
        self.blocking = blocking
        self.stream = None
        self._error_type = error_type
        self._contention_error_type = contention_error_type or (
            FileLockContentionError if error_type is FileLockError else error_type
        )

    def acquire(self):
        if self.path.is_symlink():
            raise self._error_type('监控锁不能是符号链接。')
        self.stream = self.path.open('a+b')
        if self.stream.seek(0, 2) == 0:
            self.stream.write(b'0')
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_LOCK if self.blocking else msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB))
        except OSError:
            self.stream.close()
            self.stream = None
            if self.blocking:
                raise self._contention_error_type('无法锁定监控记录。') from None
            return False
        return True

    def release(self):
        if self.stream is not None:
            try:
                self.stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            finally:
                self.stream.close()
                self.stream = None
