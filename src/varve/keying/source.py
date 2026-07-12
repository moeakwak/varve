"""File-granularity Python source observation."""

from __future__ import annotations

import ast
import inspect
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from varve.dependencies import merge_dependencies
from varve.keying.fingerprint import assert_no_symlink_path, json_sha256
from varve.models import SourceFingerprint, SourceManifestEntry


def _definition_file(value: Any, description: str) -> Path:
    source = inspect.getsourcefile(value)
    if source is None:
        raise ValueError(f"Cannot locate Python source file for {description}")
    source_path = Path(source)
    assert_no_symlink_path(source_path, description="source paths")
    path = source_path.resolve()
    if not path.is_file():
        raise ValueError(f"Python source file does not exist for {description}: {path}")
    return path


def _source_roots(pipeline_type: type[Any], stage_spec: Any) -> tuple[tuple[str, Path], ...]:
    pipeline_file = _definition_file(pipeline_type, f"pipeline {pipeline_type.__name__}")
    stage_file = _definition_file(stage_spec.func, f"stage {stage_spec.name}")
    depends = merge_dependencies(pipeline_type.depends, stage_spec.depends)
    declaration_base = pipeline_file.parent
    explicit = {
        (path if path.is_absolute() else declaration_base / path).expanduser().absolute()
        for path in depends.sources
    }
    roots = [("pipeline", pipeline_file), ("stage", stage_file)]
    for path in sorted(explicit, key=str):
        try:
            display = path.relative_to(declaration_base).as_posix()
        except ValueError:
            display = path.as_posix()
        roots.append((f"declared:{display}", path))
    seen: set[Path] = set()
    unique = []
    for label, path in roots:
        if path in seen:
            continue
        seen.add(path)
        unique.append((label, path))
    return tuple(unique)


def _collect_python_files(root: Path) -> list[tuple[str, Path]]:
    assert_no_symlink_path(root, description="source paths")
    if not root.exists():
        raise FileNotFoundError(f"Declared source path does not exist: {root}")
    if root.is_file():
        if root.suffix != ".py":
            raise ValueError(f"Declared source file must end in .py: {root}")
        return [(root.name, root)]
    if not root.is_dir():
        raise ValueError(f"Unsupported source path: {root}")
    members = []
    for member in sorted(root.rglob("*")):
        if member.is_symlink():
            raise ValueError(f"Symlinks are not supported in source paths: {member}")
        if member.is_file() and member.suffix == ".py":
            members.append((member.relative_to(root).as_posix(), member))
    return members


def _source_entry(
    label: str,
    path: Path,
    cached: SourceManifestEntry | None,
    *,
    force_rehash: bool,
    retries: int = 2,
) -> SourceManifestEntry:
    cache_path = str(path.resolve())
    for attempt in range(retries + 1):
        try:
            before = path.stat()
            if (
                cached is not None
                and not force_rehash
                and cached.path == label
                and cached.cache_path == cache_path
                and cached.inode == before.st_ino
                and cached.size == before.st_size
                and cached.mtime_ns == before.st_mtime_ns
                and cached.algorithm == "ast-sha256"
            ):
                return cached
            source = path.read_bytes()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(source, filename=str(path))
            after = path.stat()
        except (OSError, SyntaxError, UnicodeError) as error:
            raise ValueError(f"Cannot parse Python source file: {path}: {error}") from error
        before_token = (before.st_ino, before.st_size, before.st_mtime_ns)
        after_token = (after.st_ino, after.st_size, after.st_mtime_ns)
        if before_token == after_token:
            return SourceManifestEntry(
                path=label,
                cache_path=cache_path,
                digest=json_sha256(ast.dump(tree, annotate_fields=True, include_attributes=False)),
                inode=after.st_ino,
                size=after.st_size,
                mtime_ns=after.st_mtime_ns,
            )
        if attempt == retries:
            raise ValueError(f"Python source file changed while fingerprinting: {path}")
    raise AssertionError("unreachable")


@dataclass
class SourceFingerprintSession:
    _cache: dict[tuple[Any, ...], SourceFingerprint] = field(default_factory=dict)
    force_rehash: bool = False

    def fingerprint(
        self,
        pipeline_type: type[Any],
        stage_spec: Any,
        *,
        cached: tuple[SourceFingerprint, ...] = (),
    ) -> SourceFingerprint:
        roots = _source_roots(pipeline_type, stage_spec)
        key = (
            pipeline_type,
            id(stage_spec.func),
            tuple((label, str(root)) for label, root in roots),
        )
        observed = self._cache.get(key)
        if observed is not None:
            return observed
        cached_entries: dict[str, SourceManifestEntry] = {}
        for fingerprint in cached:
            for entry in fingerprint.files:
                cached_entries.setdefault(entry.path, entry)
        entries: dict[str, SourceManifestEntry] = {}
        for label, root in roots:
            for relative, member in _collect_python_files(root):
                member_label = f"{label}/{relative}"
                entries[member_label] = _source_entry(
                    member_label,
                    member,
                    cached_entries.get(member_label),
                    force_rehash=self.force_rehash,
                )
        files = [entries[name] for name in sorted(entries)]
        result = SourceFingerprint(
            fingerprint=json_sha256(
                [{"path": entry.path, "digest": entry.digest} for entry in files]
            ),
            files=files,
        )
        self._cache[key] = result
        return result
