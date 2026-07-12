"""File fingerprints and canonical JSON helpers."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from varve.dependencies import JSON
from varve.models import ArtifactFingerprint, ArtifactManifestEntry, FileFingerprint


@dataclass(slots=True)
class _FileSnapshot:
    path: str
    inode: int
    size: int
    mtime_ns: int
    hashed: FileFingerprint | None = None


@dataclass
class FingerprintSession:
    """Reuse filesystem observations within one probe or run command."""

    _snapshots: dict[str, _FileSnapshot] = field(default_factory=dict)
    force_rehash: bool = False

    def fingerprint(
        self,
        path: Path,
        cached: FileFingerprint | None = None,
        *,
        cached_by_path: Mapping[str, FileFingerprint] | None = None,
        force_rehash: bool = False,
    ) -> FileFingerprint:
        expanded = path.expanduser()
        input_path = str(expanded)
        snapshot = self._snapshots.get(input_path)
        if snapshot is None:
            normalized_path = expanded.resolve()
            normalized = str(normalized_path)
            snapshot = self._snapshots.get(normalized)
            if snapshot is None:
                try:
                    stat = normalized_path.stat()
                except FileNotFoundError as error:
                    raise FileNotFoundError(
                        f"Key input file does not exist: {normalized_path}"
                    ) from error
                snapshot = _FileSnapshot(
                    path=normalized,
                    inode=stat.st_ino,
                    size=stat.st_size,
                    mtime_ns=stat.st_mtime_ns,
                )
                self._snapshots[normalized] = snapshot
            self._snapshots[input_path] = snapshot
        if snapshot.hashed is not None and not force_rehash and not self.force_rehash:
            return snapshot.hashed
        if cached is None and cached_by_path is not None:
            cached = cached_by_path.get(snapshot.path)
        if (
            cached is not None
            and cached.path == snapshot.path
            and not self.force_rehash
            and not force_rehash
            and cached.inode == snapshot.inode
            and cached.size == snapshot.size
            and cached.mtime_ns == snapshot.mtime_ns
            and cached.algorithm == "sha256"
        ):
            return cached

        normalized_path = Path(snapshot.path)
        digest, stable_stat = _stable_sha256_file(normalized_path)
        snapshot.inode = stable_stat.st_ino
        snapshot.size = stable_stat.st_size
        snapshot.mtime_ns = stable_stat.st_mtime_ns
        result = FileFingerprint(
            path=snapshot.path,
            inode=snapshot.inode,
            size=snapshot.size,
            mtime_ns=snapshot.mtime_ns,
            content_hash=digest,
        )
        snapshot.hashed = result
        return result


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


def _stable_sha256_file(path: Path, *, retries: int = 2) -> tuple[str, os.stat_result]:
    for attempt in range(retries + 1):
        before = path.stat()
        digest = _sha256_file(path)
        after = path.stat()
        token_before = (before.st_ino, before.st_size, before.st_mtime_ns)
        token_after = (after.st_ino, after.st_size, after.st_mtime_ns)
        if token_before == token_after:
            return digest, after
        if attempt == retries:
            raise OSError(f"File changed while hashing: {path}")
    raise AssertionError("unreachable")


def _normalize_for_json(value: Any) -> JSON:
    if isinstance(value, BaseModel):
        return _normalize_for_json(value.model_dump(mode="json"))
    if isinstance(value, dict):
        normalized: dict[str, JSON] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(
                    "JSON objects used in varve keys must have string keys; "
                    f"got {type(key).__name__}"
                )
            normalized[key] = _normalize_for_json(item)
        return normalized
    if isinstance(value, list | tuple):
        return [_normalize_for_json(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    raise TypeError(f"Value is not JSON-serializable for varve keys: {type(value).__name__}")


def canonical_json(obj: Any) -> bytes:
    normalized = _normalize_for_json(obj)
    return json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def json_sha256(obj: Any) -> str:
    digest = hashlib.sha256(canonical_json(obj)).hexdigest()
    return f"sha256:{digest}"


def file_digest_view(
    files: Mapping[str, list[FileFingerprint]],
) -> dict[str, str]:
    """Project file fingerprints onto the digests used by input keys."""

    return {
        name: json_sha256(
            [
                {"path": member.path, "kind": member.kind, "content_hash": member.content_hash}
                for member in sorted(members, key=lambda item: item.path)
            ]
        )
        for name, members in sorted(files.items())
    }


def file_fingerprint(
    path: Path,
    cached: FileFingerprint | None = None,
    *,
    session: FingerprintSession | None = None,
) -> FileFingerprint:
    return (session or FingerprintSession()).fingerprint(path, cached)


def _coerce_paths(value: Path | Sequence[Path]) -> list[Path]:
    if isinstance(value, Path):
        return [value]
    return list(value)


def assert_no_symlink_path(path: Path, *, description: str) -> None:
    """Reject a symlink at any component of an explicitly supplied path."""

    absolute = path.expanduser().absolute()
    for component in reversed((absolute, *absolute.parents)):
        if component.is_symlink():
            raise ValueError(f"Symlinks are not supported in {description}: {component}")


def files_fingerprints(
    ctx: Any,
    files_spec: Mapping[str, Callable[[Any], Path | Sequence[Path]]],
    cached_by_name: Mapping[str, list[FileFingerprint]] | None = None,
    *,
    session: FingerprintSession | None = None,
) -> dict[str, list[FileFingerprint]]:
    cached_by_name = cached_by_name or {}
    session = session or FingerprintSession()
    results: dict[str, list[FileFingerprint]] = {}
    for name, resolve_paths in sorted(files_spec.items()):
        cached_by_path = {item.path: item for item in cached_by_name.get(name, [])}
        members = {}
        for path in _coerce_paths(resolve_paths(ctx)):
            assert_no_symlink_path(path, description="input dependencies")
            expanded = path.expanduser().resolve()
            if not expanded.exists():
                raise FileNotFoundError(f"Input dependency does not exist: {expanded}")
            if expanded.is_file():
                fingerprint = session.fingerprint(expanded, cached_by_path=cached_by_path)
                members[fingerprint.path] = fingerprint
                continue
            if not expanded.is_dir():
                raise ValueError(f"Unsupported input dependency: {expanded}")
            for member in (expanded, *sorted(expanded.rglob("*"))):
                if member.is_symlink():
                    raise ValueError(f"Symlinks are not supported in input dependencies: {member}")
                if member.is_file():
                    fingerprint = session.fingerprint(member, cached_by_path=cached_by_path)
                elif member.is_dir():
                    stat = member.stat()
                    fingerprint = FileFingerprint(
                        path=str(member),
                        kind="dir",
                        inode=stat.st_ino,
                        size=0,
                        mtime_ns=stat.st_mtime_ns,
                        content_hash=json_sha256({"entry": "dir"}),
                    )
                else:
                    raise ValueError(f"Unsupported input dependency entry: {member}")
                members[fingerprint.path] = fingerprint
        results[name] = [members[path] for path in sorted(members)]
    return results


def artifact_fingerprint(
    path: Path,
    output_root: Path,
    *,
    cached: ArtifactFingerprint | None = None,
    session: FingerprintSession | None = None,
    force_rehash: bool = False,
) -> ArtifactFingerprint:
    """Fingerprint a managed file or directory tree by semantic content."""

    # A commit fingerprint must observe the filesystem after the stage body.
    # Reusing the command session here could retain a pre-execution stat token
    # or content hash for a path the stage rewrote in place.
    session = (
        FingerprintSession(force_rehash=True) if force_rehash else session or FingerprintSession()
    )
    assert_no_symlink_path(path, description="managed artifacts")
    resolved = path.resolve()
    root = output_root.resolve()
    try:
        relative_root = resolved.relative_to(root).as_posix()
    except ValueError as error:
        raise ValueError(f"Managed artifact must be under the output root: {resolved}") from error
    if not resolved.exists():
        raise FileNotFoundError(f"Managed artifact does not exist: {resolved}")
    cached_files = (
        {
            entry.fingerprint.path: entry.fingerprint
            for entry in cached.manifest
            if entry.fingerprint is not None
        }
        if cached is not None
        else {}
    )
    entries: list[ArtifactManifestEntry] = []
    if resolved.is_file():
        fingerprint = session.fingerprint(
            resolved,
            cached_by_path=cached_files,
            force_rehash=force_rehash,
        )
        entries.append(ArtifactManifestEntry(path=".", kind="file", fingerprint=fingerprint))
        kind = "file"
    elif resolved.is_dir():
        kind = "dir"
        entries.append(ArtifactManifestEntry(path=".", kind="dir"))
        for member in sorted(resolved.rglob("*")):
            if member.is_symlink():
                raise ValueError(f"Symlinks are not supported in managed artifacts: {member}")
            relative = member.relative_to(resolved).as_posix()
            if member.is_dir():
                entries.append(ArtifactManifestEntry(path=relative, kind="dir"))
            elif member.is_file():
                entries.append(
                    ArtifactManifestEntry(
                        path=relative,
                        kind="file",
                        fingerprint=session.fingerprint(
                            member,
                            cached_by_path=cached_files,
                            force_rehash=force_rehash,
                        ),
                    )
                )
            else:
                raise ValueError(f"Unsupported managed artifact entry: {member}")
    else:
        raise ValueError(f"Unsupported managed artifact entry: {resolved}")
    digest_view = [
        {
            "path": entry.path,
            "kind": entry.kind,
            **({"content_hash": entry.fingerprint.content_hash} if entry.fingerprint else {}),
        }
        for entry in entries
    ]
    return ArtifactFingerprint(
        root=relative_root,
        kind=kind,
        manifest=entries,
        fingerprint=json_sha256(digest_view),
    )


def artifacts_root_fingerprint(
    artifacts: list[ArtifactFingerprint],
    *,
    positions: list[tuple[int, ...]] | None = None,
) -> str:
    """Fingerprint the ordered artifact handles exposed by one stage.

    Individual artifact fingerprints describe filesystem content.  This root
    fingerprint additionally preserves the handle order observed by
    ``Ctx.inputs()``.  Batch callers pass ``(index, ordinal)`` positions;
    ordinary stages use the implicit declaration ordinal.
    """

    if positions is None:
        positions = [(ordinal,) for ordinal in range(len(artifacts))]
    if len(positions) != len(artifacts):
        raise ValueError("Artifact positions must match the artifact list")
    return json_sha256(
        [
            {
                "position": list(position),
                "root": item.root,
                "kind": item.kind,
                "fingerprint": item.fingerprint,
            }
            for position, item in zip(positions, artifacts)
        ]
    )
