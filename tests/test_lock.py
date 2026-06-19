from __future__ import annotations

import os
from pathlib import Path

import pytest

from varve.store.lock import OutputLock, is_stale_lock


def test_output_lock_excludes_other_writers(tmp_path: Path) -> None:
    varve_root = tmp_path / ".varve"
    with OutputLock(varve_root):
        with pytest.raises(RuntimeError, match="already locked"):
            with OutputLock(varve_root):
                pass
    with OutputLock(varve_root):
        pass


def test_stale_lock_detection(tmp_path: Path) -> None:
    varve_root = tmp_path / ".varve"
    varve_root.mkdir()
    (varve_root / "lock").write_text(str(os.getpid() + 10_000_000), encoding="utf-8")
    assert is_stale_lock(varve_root)
