from __future__ import annotations

from pathlib import Path

import pytest

from varve import file_set


def test_file_set_returns_sorted_unique_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "b.txt").write_text("b", encoding="utf-8")
    (root / "a.txt").write_text("a", encoding="utf-8")

    resolve = file_set(root=root, include=["*.txt", "a.*"])

    assert [path.name for path in resolve(None)] == ["a.txt", "b.txt"]


def test_file_set_ignores_directories(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "nested.txt").mkdir()

    resolve = file_set(root=lambda _ctx: root, include=["*.txt"])

    assert [path.name for path in resolve(None)] == ["a.txt"]


def test_file_set_fails_when_root_is_missing(tmp_path: Path) -> None:
    resolve = file_set(root=tmp_path / "missing", include=["*.txt"])

    with pytest.raises(FileNotFoundError, match="root does not exist"):
        resolve(None)


def test_file_set_fails_when_pattern_matches_no_files(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    resolve = file_set(root=root, include=["*.txt"])

    with pytest.raises(FileNotFoundError, match="matched no files"):
        resolve(None)


def test_file_set_can_allow_empty_patterns(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    resolve = file_set(root=root, include=["*.txt"], allow_empty=True)

    assert resolve(None) == []
