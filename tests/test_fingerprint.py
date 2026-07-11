from __future__ import annotations

import json
from pathlib import Path

import pytest

from varve.keying import fingerprint as fingerprint_module
from varve.keying.fingerprint import (
    FingerprintSession,
    canonical_json,
    file_fingerprint,
    files_fingerprints,
)


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
    assert canonical_json({"b": 2, "a": [1.0, True]}) == canonical_json({"a": [1.0, True], "b": 2})
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
    assert [item.sha256 for item in one["datasets"]] == [item.sha256 for item in two["datasets"]]


def test_fingerprint_session_shares_path_snapshot_and_keeps_files_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_path = tmp_path / "first.txt"
    second_path = tmp_path / "second.txt"
    first_path.write_text("first", encoding="utf-8")
    second_path.write_text("second", encoding="utf-8")
    cached = file_fingerprint(first_path)
    stale = cached.model_copy(update={"mtime": cached.mtime - 1, "sha256": "sha256:stale"})
    # Python 3.10 Path.resolve() calls Path.stat() internally. Resolve first so
    # this test counts only the session's explicit stat calls on every version.
    resolved_paths = {
        first_path: first_path.resolve(),
        second_path: second_path.resolve(),
    }

    resolve_calls: list[Path] = []
    stat_calls: list[Path] = []
    hash_calls: list[Path] = []
    original_stat = Path.stat
    original_hash = fingerprint_module._sha256_file

    def counted_resolve(path: Path, *args, **kwargs):
        del args, kwargs
        resolve_calls.append(path)
        return resolved_paths[path]

    def counted_stat(path: Path, *args, **kwargs):
        stat_calls.append(path)
        return original_stat(path, *args, **kwargs)

    def counted_hash(path: Path) -> str:
        hash_calls.append(path)
        return original_hash(path)

    monkeypatch.setattr(Path, "resolve", counted_resolve)
    monkeypatch.setattr(Path, "stat", counted_stat)
    monkeypatch.setattr(fingerprint_module, "_sha256_file", counted_hash)

    session = FingerprintSession()
    refreshed = file_fingerprint(first_path, cached=stale, session=session)
    shared = file_fingerprint(first_path, cached=cached, session=session)
    distinct = file_fingerprint(second_path, session=session)

    assert shared is refreshed
    assert refreshed.sha256 == cached.sha256
    assert distinct.sha256 != refreshed.sha256
    assert resolve_calls == [first_path, second_path]
    assert stat_calls == [first_path, second_path]
    assert hash_calls == [first_path, second_path]


def test_fingerprint_session_keeps_stage_cached_fingerprints_independent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "input.txt"
    path.write_text("content", encoding="utf-8")
    first = file_fingerprint(path)
    second = first.model_copy(update={"sha256": "sha256:other-stage-record"})
    session = FingerprintSession()

    def unexpected_hash(_path: Path) -> str:
        raise AssertionError("matching cached fingerprints must not read file content")

    monkeypatch.setattr(fingerprint_module, "_sha256_file", unexpected_hash)

    assert file_fingerprint(path, cached=first, session=session) is first
    assert file_fingerprint(path, cached=second, session=session) is second
