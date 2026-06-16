from __future__ import annotations

import json
from pathlib import Path

import pytest

from varve.fingerprint import canonical_json, file_fingerprint, files_fingerprints


def test_file_fingerprint_reuses_cached_sha_when_size_and_mtime_match(tmp_path: Path) -> None:
    path = tmp_path / "input.txt"
    path.write_text("alpha", encoding="utf-8")
    first = file_fingerprint(path)
    reused = file_fingerprint(path, cached=first)
    assert reused is first


def test_file_fingerprint_recomputes_when_mtime_changes(tmp_path: Path) -> None:
    path = tmp_path / "input.txt"
    path.write_text("alpha", encoding="utf-8")
    first = file_fingerprint(path)
    path.write_text("bravo", encoding="utf-8")
    second = file_fingerprint(path, cached=first)
    assert second.sha256 != first.sha256


def test_file_fingerprint_missing_file_fails_fast(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        file_fingerprint(tmp_path / "missing.txt")


def test_canonical_json_is_stable_and_rejects_unknown_objects() -> None:
    assert canonical_json({"b": 2, "a": [1.0, True]}) == canonical_json(
        {"a": [1.0, True], "b": 2}
    )
    with pytest.raises(TypeError, match="string keys"):
        canonical_json({1: "not-json"})
    with pytest.raises(TypeError):
        canonical_json({"path": Path("not-json")})
    assert json.loads(canonical_json({"x": 1}).decode("utf-8")) == {"x": 1}


def test_files_fingerprints_are_order_independent(tmp_path: Path) -> None:
    a = tmp_path / "a.txt"
    b = tmp_path / "b.txt"
    a.write_text("a", encoding="utf-8")
    b.write_text("b", encoding="utf-8")

    class Ctx:
        pass

    one = files_fingerprints(Ctx(), {"datasets": lambda _ctx: [b, a]})
    two = files_fingerprints(Ctx(), {"datasets": lambda _ctx: [a, b]})
    assert [item.sha256 for item in one["datasets"]] == [
        item.sha256 for item in two["datasets"]
    ]
