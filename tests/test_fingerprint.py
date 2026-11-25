from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from varve import Dependencies
from varve.keying import fingerprint as fingerprint_module
from varve.keying.fingerprint import (
    FingerprintSession,
    StatCache,
    artifact_fingerprint,
    canonical_json,
    glob_match,
    verify_manifest,
    walk_files,
)


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def test_canonical_json_is_stable_and_strict() -> None:
    class Model(BaseModel):
        b: int = 1
        a: str = "x"

    assert canonical_json({"b": 1, "a": [1, (2, 3)]}) == b'{"a":[1,[2,3]],"b":1}'
    assert canonical_json(Model()) == b'{"a":"x","b":1}'
    assert canonical_json(Path("a/b")) == b'"a/b"'
    with pytest.raises(TypeError, match="not JSON-serializable"):
        canonical_json(object())
    with pytest.raises(TypeError, match="string keys"):
        canonical_json({1: "x"})


@pytest.mark.parametrize(
    ("relpath", "pattern", "expected"),
    [
        ("a.py", "**/*.py", True),
        ("x/y/a.py", "**/*.py", True),
        ("x/a.py", "*.py", False),
        ("x/a.py", "x/*.py", True),
        ("x/a.txt", "**", True),
        ("a1.py", "a?.py", True),
        ("ab.py", "a[!b].py", False),
        ("x/cache/a.py", "**/cache/**", True),
    ],
)
def test_glob_match(relpath: str, pattern: str, expected: bool) -> None:
    assert glob_match(relpath, pattern) is expected


def test_input_material_uses_relative_paths(tmp_path: Path) -> None:
    for root in ("one", "two"):
        write(tmp_path / root / "data" / "a.txt", "alpha")
        write(tmp_path / root / "data" / "sub" / "b.txt", "beta")
    session = FingerprintSession()
    one = session.input_digest(tmp_path / "one" / "data")
    two = session.input_digest(str(tmp_path / "two" / "data"))
    assert one.digest == two.digest
    assert one.files == 2 and one.bytes == 9
    assert [row[0] for row in one.material[0][1]] == ["a.txt", "sub/b.txt"]
    assert one.roots == [str(tmp_path / "one" / "data")]

    (tmp_path / "two" / "data" / "a.txt").rename(tmp_path / "two" / "data" / "c.txt")
    assert FingerprintSession().input_digest(tmp_path / "two" / "data").digest != one.digest


def test_content_and_stat_modes(tmp_path: Path) -> None:
    path = write(tmp_path / "big.bin", "payload")
    content = FingerprintSession().input_digest(path)
    stat = FingerprintSession().input_digest(Dependencies.stat(path))
    assert stat.material[0][0] == "stat" and stat.material[0][1] == [
        ["big.bin", 7, path.stat().st_mtime_ns]
    ]
    os.utime(path, ns=(1, 1))
    assert FingerprintSession().input_digest(path).digest == content.digest
    assert FingerprintSession().input_digest(Dependencies.stat(path)).digest != stat.digest


def test_glob_sets(tmp_path: Path) -> None:
    write(tmp_path / "src" / "a.py", "a")
    write(tmp_path / "src" / "pkg" / "b.py", "b")
    write(tmp_path / "src" / "pkg" / "__pycache__" / "b.pyc", "c")
    write(tmp_path / "src" / "notes.md", "d")
    spec = Dependencies.glob(tmp_path / "src", include="**/*.py", exclude=["**/__pycache__/**"])
    digest = FingerprintSession().input_digest(spec)
    assert [row[0] for row in digest.material[0][1]] == ["a.py", "pkg/b.py"]
    stat_glob = Dependencies.stat(spec)
    assert FingerprintSession().input_digest(stat_glob).material[0][0] == "stat"
    with pytest.raises(FileNotFoundError, match="matched no files"):
        FingerprintSession().input_digest(Dependencies.glob(tmp_path / "src", include="*.rs"))
    empty = Dependencies.glob(tmp_path / "src", include="*.rs", allow_empty=True)
    assert FingerprintSession().input_digest(empty).files == 0
    with pytest.raises(FileNotFoundError, match="does not exist"):
        FingerprintSession().input_digest(Dependencies.glob(tmp_path / "missing"))


def test_root_symlink_prefix_allowed_but_not_inside_trees(tmp_path: Path) -> None:
    write(tmp_path / "real" / "a.txt", "a")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    assert [rel for rel, _ in walk_files(tmp_path / "link")] == ["a.txt"]
    (tmp_path / "file-link").symlink_to(tmp_path / "real" / "a.txt")
    assert [rel for rel, _ in walk_files(tmp_path / "file-link")] == ["a.txt"]
    (tmp_path / "real" / "inner").symlink_to(tmp_path / "real" / "a.txt")
    with pytest.raises(ValueError, match="Symlinks are not allowed"):
        walk_files(tmp_path / "link")


def test_stat_cache_avoids_rehashing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write(tmp_path / "input.txt", "alpha")
    cache = StatCache()
    first = FingerprintSession(cache).input_digest(path)
    rows = cache.drain()
    assert list(rows) == [str(path.resolve())] and cache.drain() == {}

    calls: list[Path] = []
    real = fingerprint_module._sha256_file

    def counting(target: Path) -> str:
        calls.append(target)
        return real(target)

    monkeypatch.setattr(fingerprint_module, "_sha256_file", counting)
    assert FingerprintSession(StatCache(rows)).input_digest(path).digest == first.digest
    assert calls == []
    FingerprintSession(StatCache(rows), rehash=True).input_digest(path)
    assert len(calls) == 1
    path.write_text("beta!")
    assert FingerprintSession(StatCache(rows)).input_digest(path).digest != first.digest
    assert len(calls) == 2


def test_hash_retries_when_file_changes_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = write(tmp_path / "input.txt", "alpha")
    real = fingerprint_module._sha256_file
    attempts: list[int] = []

    def racing(target: Path) -> str:
        if not attempts:
            target.write_text("changed during hashing")
        attempts.append(1)
        return real(target)

    monkeypatch.setattr(fingerprint_module, "_sha256_file", racing)
    digest, st = FingerprintSession().file_hash(path.resolve())
    assert len(attempts) == 2 and st.st_size == len("changed during hashing")
    assert digest == real(path)


def test_artifact_manifest_and_verification(tmp_path: Path) -> None:
    unit = tmp_path / "unit"
    write(unit / "part.parquet", "rows")
    write(unit / "img" / "a.png", "png")
    artifact = artifact_fingerprint(unit)
    assert [row[0] for row in artifact.manifest] == ["img/a.png", "part.parquet"]
    assert artifact.files == 2 and artifact.bytes == 7
    assert verify_manifest(unit, artifact.manifest)

    copy = tmp_path / "copy"
    write(copy / "part.parquet", "rows")
    write(copy / "img" / "a.png", "png")
    assert artifact_fingerprint(copy).fingerprint == artifact.fingerprint

    target = unit / "part.parquet"
    stat = target.stat()
    target.write_text("ROWS")
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert verify_manifest(unit, artifact.manifest)  # stat check cannot see it
    assert not verify_manifest(unit, artifact.manifest, rehash=True)
    target.write_text("longer rows")
    assert not verify_manifest(unit, artifact.manifest)
    target.unlink()
    assert not verify_manifest(unit, artifact.manifest)


def test_inputs_are_sorted_by_name(tmp_path: Path) -> None:
    write(tmp_path / "a.txt", "a")
    specs: dict[str, Any] = {"zeta": tmp_path / "a.txt", "alpha": [tmp_path / "a.txt"]}
    assert list(FingerprintSession().inputs(specs)) == ["alpha", "zeta"]


def test_glob_ignores_symlinks_it_cannot_select(tmp_path: Path) -> None:
    write(tmp_path / "src" / "a.rs", "a")
    write(tmp_path / "elsewhere" / "b.rs", "b")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "link").symlink_to(tmp_path / "elsewhere")
    (tmp_path / ".venv").symlink_to(tmp_path / "elsewhere")
    (tmp_path / "notes.md").symlink_to(tmp_path / "src" / "a.rs")
    spec = Dependencies.glob(tmp_path, include="src/**/*.rs")
    assert [row[0] for row in FingerprintSession().input_digest(spec).material[0][1]] == [
        "src/a.rs"
    ]


def test_glob_rejects_selected_symlink_file(tmp_path: Path) -> None:
    write(tmp_path / "src" / "a.rs", "a")
    (tmp_path / "src" / "b.rs").symlink_to(tmp_path / "src" / "a.rs")
    with pytest.raises(ValueError, match="Symlinks are not allowed"):
        FingerprintSession().input_digest(Dependencies.glob(tmp_path, include="src/**/*.rs"))
    # A symlink matched by include but also by exclude is ignored.
    spec = Dependencies.glob(tmp_path, include="src/**/*.rs", exclude="src/b.rs")
    assert FingerprintSession().input_digest(spec).files == 1


def test_glob_symlinked_dir_errors_only_when_patterns_reach_it(tmp_path: Path) -> None:
    write(tmp_path / "src" / "a.rs", "a")
    write(tmp_path / "elsewhere" / "b.rs", "b")
    (tmp_path / "src" / "vendored").symlink_to(tmp_path / "elsewhere")
    (tmp_path / "node_modules").symlink_to(tmp_path / "elsewhere")
    # `**` must descend into src/, and src/vendored/x.rs could match.
    with pytest.raises(ValueError, match="Symlinks are not allowed"):
        FingerprintSession().input_digest(Dependencies.glob(tmp_path, include="src/**/*.rs"))
    # A pattern that cannot select below src/vendored leaves it alone.
    top = Dependencies.glob(tmp_path, include="src/*.rs")
    assert FingerprintSession().input_digest(top).files == 1
    # Under a root-level `**`, node_modules could contribute unless excluded.
    with pytest.raises(ValueError, match="node_modules"):
        FingerprintSession().input_digest(
            Dependencies.glob(tmp_path, include="**/*.rs", exclude="src/vendored")
        )


def test_glob_excluded_symlinks_are_fine(tmp_path: Path) -> None:
    write(tmp_path / "src" / "a.rs", "a")
    write(tmp_path / "elsewhere" / "b.rs", "b")
    (tmp_path / "src" / "vendored").symlink_to(tmp_path / "elsewhere")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "node_modules").symlink_to(tmp_path / "elsewhere")
    spec = Dependencies.glob(
        tmp_path,
        include="**/*.rs",
        exclude=["src/vendored", "**/node_modules/**", "elsewhere/**"],
    )
    assert [row[0] for row in FingerprintSession().input_digest(spec).material[0][1]] == [
        "src/a.rs"
    ]
