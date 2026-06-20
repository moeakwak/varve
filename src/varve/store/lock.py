"""Single-writer output-root lock."""

from __future__ import annotations

import errno
import fcntl
import os
from pathlib import Path


def _lock_payload() -> bytes:
    return b"locked\n"


def _try_file_lock(fd: int) -> bool:
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EAGAIN):
            return False
        raise
    return True


def _unlock_file(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)


def is_stale_lock(varve_root: Path) -> bool:
    path = varve_root / "lock"
    try:
        fd = os.open(path, os.O_RDWR)
    except FileNotFoundError:
        return False
    try:
        if not _try_file_lock(fd):
            return False
        _unlock_file(fd)
        return True
    finally:
        os.close(fd)


class OutputLock:
    def __init__(self, varve_root: Path) -> None:
        self.varve_root = varve_root
        self.path = varve_root / "lock"
        self._fd: int | None = None

    def __enter__(self) -> OutputLock:
        self.varve_root.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        if not _try_file_lock(self._fd):
            os.close(self._fd)
            self._fd = None
            raise RuntimeError(f"Varve output root is already locked: {self.path}")
        try:
            os.ftruncate(self._fd, 0)
            os.write(self._fd, _lock_payload())
        except Exception:
            _unlock_file(self._fd)
            os.close(self._fd)
            self._fd = None
            raise
        return self

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            try:
                stat = os.fstat(self._fd)
                try:
                    path_stat = self.path.stat()
                except FileNotFoundError:
                    path_stat = None
                if path_stat is not None and (
                    path_stat.st_dev,
                    path_stat.st_ino,
                ) == (stat.st_dev, stat.st_ino):
                    self.path.unlink()
            finally:
                _unlock_file(self._fd)
                os.close(self._fd)
                self._fd = None
