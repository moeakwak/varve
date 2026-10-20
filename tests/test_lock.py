from __future__ import annotations

import os
from pathlib import Path

import pytest

from varve.store import lock as lock_module
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


def test_lock_acquired_on_released_inode_is_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    varve_root = tmp_path / ".varve"
    holder = OutputLock(varve_root)
    holder.__enter__()
    real_open = os.open
    calls = 0

    def open_then_release(*args, **kwargs):
        nonlocal calls
        calls += 1
        fd = real_open(*args, **kwargs)
        if calls == 1:
            # The holder releases after this writer opened the old inode.
            holder.__exit__(None, None, None)
        return fd

    monkeypatch.setattr(lock_module.os, "open", open_then_release)
    with OutputLock(varve_root):
        monkeypatch.setattr(lock_module.os, "open", real_open)
        assert calls == 2
        with pytest.raises(RuntimeError, match="already locked"):
            with OutputLock(varve_root):
                pass
