"""Single-writer output-root lock."""

from __future__ import annotations

import os
from pathlib import Path


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def is_stale_lock(varve_root: Path) -> bool:
    path = varve_root / "lock"
    if not path.exists():
        return False
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
    except ValueError:
        return True
    return not _pid_alive(pid)


class OutputLock:
    def __init__(self, varve_root: Path) -> None:
        self.varve_root = varve_root
        self.path = varve_root / "lock"
        self._fd: int | None = None

    def __enter__(self) -> OutputLock:
        self.varve_root.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            self._fd = os.open(self.path, flags)
        except FileExistsError as error:
            if is_stale_lock(self.varve_root):
                self.path.unlink(missing_ok=True)
                self._fd = os.open(self.path, flags)
            else:
                raise RuntimeError(f"Varve output root is already locked: {self.path}") from error
        os.write(self._fd, str(os.getpid()).encode("utf-8"))
        return self

    def __exit__(self, *exc) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None
        self.path.unlink(missing_ok=True)
