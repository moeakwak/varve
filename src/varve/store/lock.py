"""Advisory ``fcntl`` locks on ``.varve/lock`` and ``.varve/locks/<branch>.lock``.

``run`` / ``reuse`` / branch ``clean`` hold the pipeline lock shared and the
branch lock exclusive; ``clean --storage`` / ``--orphans`` / whole clean hold
the pipeline lock exclusive. Lock files are never unlinked.
"""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path
from types import TracebackType

from varve.store import layout


class LockBusyError(RuntimeError):
    """Another varve command holds a conflicting lock."""


class FileLock:
    def __init__(self, path: Path, *, exclusive: bool, blocking: bool = False) -> None:
        self.path = path
        self.exclusive = exclusive
        self.blocking = blocking
        self._fd: int | None = None

    def acquire(self) -> FileLock:
        if self._fd is not None:
            raise RuntimeError(f"Lock already held: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o644)
        flags = fcntl.LOCK_EX if self.exclusive else fcntl.LOCK_SH
        if not self.blocking:
            flags |= fcntl.LOCK_NB
        try:
            fcntl.flock(fd, flags)
        except OSError as error:
            os.close(fd)
            if error.errno in (errno.EAGAIN, errno.EACCES):
                kind = "exclusive" if self.exclusive else "shared"
                raise LockBusyError(
                    f"Cannot take {kind} lock {self.path}: another varve command is using it"
                ) from None
            raise
        self._fd = fd
        return self

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None

    @property
    def held(self) -> bool:
        return self._fd is not None

    def __enter__(self) -> FileLock:
        return self.acquire()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.release()


def pipeline_lock(root: Path, *, exclusive: bool) -> FileLock:
    return FileLock(layout.pipeline_lock_path(root), exclusive=exclusive)


def branch_lock(root: Path, branch: str) -> FileLock:
    return FileLock(layout.branch_lock_path(root, branch), exclusive=True)
