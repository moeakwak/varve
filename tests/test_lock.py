from __future__ import annotations

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


def test_unlocked_existing_marker_is_stale(tmp_path: Path) -> None:
    varve_root = tmp_path / ".varve"
    varve_root.mkdir()
    (varve_root / "lock").write_text("locked\n", encoding="utf-8")
    assert is_stale_lock(varve_root)


def test_existing_lock_file_without_file_lock_is_reused(tmp_path: Path) -> None:
    varve_root = tmp_path / ".varve"
    varve_root.mkdir()
    (varve_root / "lock").write_text("legacy marker\n", encoding="utf-8")

    with OutputLock(varve_root):
        pass
