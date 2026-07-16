from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from varve.keying import fingerprint as fingerprint_module
from varve.keying.fingerprint import (
    FingerprintSession,
    artifact_fingerprint,
    artifacts_root_fingerprint,
    canonical_json,
    file_fingerprint,
    files_fingerprints,
)


def test_materialization_fingerprint_preserves_batch_positions(tmp_path: Path) -> None:
    first_path = tmp_path / "first.txt"
    second_path = tmp_path / "second.txt"
    first_path.write_text("first", encoding="utf-8")
    second_path.write_text("second", encoding="utf-8")
    artifacts = [
        artifact_fingerprint(first_path, tmp_path),
        artifact_fingerprint(second_path, tmp_path),
    ]

    ordered = artifacts_root_fingerprint(artifacts, positions=[(0, 0), (1, 0)])
    reassigned = artifacts_root_fingerprint(artifacts, positions=[(1, 0), (0, 0)])

    assert ordered != reassigned


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
    assert second.content_hash != first.content_hash


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
    assert [item.content_hash for item in one["datasets"]] == [
        item.content_hash for item in two["datasets"]
    ]


def test_fingerprint_session_reuses_input_expansion_but_calls_resolver(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "inputs"
    root.mkdir()
    (root / "value.txt").write_text("value", encoding="utf-8")
    resolver_calls = 0
    expansion_calls = 0
    original_tree_entries = fingerprint_module._tree_entries

    def resolve(_ctx):
        nonlocal resolver_calls
        resolver_calls += 1
        return root

    def counted_tree_entries(*args, **kwargs):
        nonlocal expansion_calls
        expansion_calls += 1
        yield from original_tree_entries(*args, **kwargs)

    monkeypatch.setattr(fingerprint_module, "_tree_entries", counted_tree_entries)
    session = FingerprintSession()

    first = files_fingerprints(object(), {"input": resolve}, session=session)
    second = files_fingerprints(object(), {"input": resolve}, session=session)

    assert first == second
    assert resolver_calls == 2
    assert expansion_calls == 1


def test_new_fingerprint_session_observes_input_tree_changes(tmp_path: Path) -> None:
    root = tmp_path / "inputs"
    root.mkdir()
    (root / "first.txt").write_text("first", encoding="utf-8")

    def resolver(_ctx):
        return root

    session = FingerprintSession()

    first = files_fingerprints(object(), {"input": resolver}, session=session)
    (root / "second.txt").write_text("second", encoding="utf-8")
    shared_snapshot = files_fingerprints(object(), {"input": resolver}, session=session)
    refreshed = files_fingerprints(
        object(),
        {"input": resolver},
        session=FingerprintSession(),
    )

    assert len(first["input"]) == 2
    assert shared_snapshot == first
    assert len(refreshed["input"]) == 3


def test_fingerprint_session_shares_path_snapshot_and_keeps_files_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_path = tmp_path / "first.txt"
    second_path = tmp_path / "second.txt"
    first_path.write_text("first", encoding="utf-8")
    second_path.write_text("second", encoding="utf-8")
    cached = file_fingerprint(first_path)
    stale = cached.model_copy(
        update={"mtime_ns": cached.mtime_ns - 1, "content_hash": "sha256:stale"}
    )
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
    assert refreshed.content_hash == cached.content_hash
    assert distinct.content_hash != refreshed.content_hash
    assert resolve_calls == [first_path, second_path]
    assert stat_calls.count(first_path) == 3
    assert stat_calls.count(second_path) == 3
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


def test_commit_artifact_force_rehash_ignores_preexecution_session(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("before", encoding="utf-8")
    session = FingerprintSession()
    before = artifact_fingerprint(artifact, tmp_path, session=session)

    artifact.write_text("after!", encoding="utf-8")
    committed = artifact_fingerprint(
        artifact,
        tmp_path,
        cached=before,
        session=session,
        force_rehash=True,
    )

    assert committed.fingerprint != before.fingerprint


def test_directory_artifact_fingerprint_tracks_tree_shape_and_empty_directories(
    tmp_path: Path,
) -> None:
    root = tmp_path / "tree"
    root.mkdir()
    empty = artifact_fingerprint(root, tmp_path)

    (root / "empty").mkdir()
    with_empty_dir = artifact_fingerprint(root, tmp_path)
    assert with_empty_dir.fingerprint != empty.fingerprint

    (root / "value.txt").write_text("value", encoding="utf-8")
    with_file = artifact_fingerprint(root, tmp_path)
    assert with_file.fingerprint != with_empty_dir.fingerprint

    (root / "value.txt").rename(root / "renamed.txt")
    renamed = artifact_fingerprint(root, tmp_path)
    assert renamed.fingerprint != with_file.fingerprint


def test_force_rehash_detects_content_changed_with_restored_stat(tmp_path: Path) -> None:
    artifact = tmp_path / "artifact.txt"
    artifact.write_text("before", encoding="utf-8")
    before_stat = artifact.stat()
    before = artifact_fingerprint(artifact, tmp_path)

    artifact.write_text("after!", encoding="utf-8")
    artifact.chmod(before_stat.st_mode)
    artifact.touch()
    os.utime(artifact, ns=(before_stat.st_atime_ns, before_stat.st_mtime_ns))
    after = artifact_fingerprint(artifact, tmp_path, cached=before, force_rehash=True)
    assert after.fingerprint != before.fingerprint


def test_input_and_artifact_root_symlinks_are_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("value", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(target)

    class Ctx:
        pass

    with pytest.raises(ValueError, match="Symlinks are not supported in input dependencies"):
        files_fingerprints(Ctx(), {"input": lambda _ctx: link})
    with pytest.raises(ValueError, match="Symlinks are not supported in managed artifacts"):
        artifact_fingerprint(link, tmp_path)


def test_file_fingerprint_retries_a_concurrent_change_and_records_final_stat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "changing.txt"
    path.write_text("before", encoding="utf-8")
    original_hash = fingerprint_module._sha256_file
    calls = 0

    def mutate_once(target: Path) -> str:
        nonlocal calls
        calls += 1
        digest = original_hash(target)
        if calls == 1:
            target.write_text("after!", encoding="utf-8")
        return digest

    monkeypatch.setattr(fingerprint_module, "_sha256_file", mutate_once)

    fingerprint = file_fingerprint(path)

    assert calls == 2
    assert fingerprint.content_hash == original_hash(path)
    assert fingerprint.size == path.stat().st_size
    assert fingerprint.mtime_ns == path.stat().st_mtime_ns
