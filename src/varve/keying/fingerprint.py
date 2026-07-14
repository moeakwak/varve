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
            normalized = str(expanded.resolve())
            snapshot = self._snapshots.get(normalized)
            if snapshot is None:
                try:
                    stat = Path(normalized).stat()
                except FileNotFoundError as error:
                    raise FileNotFoundError(
                        f"Key input file does not exist: {normalized}"
                    ) from error
                snapshot = _FileSnapshot(normalized, stat.st_ino, stat.st_size, stat.st_mtime_ns)
                self._snapshots[normalized] = snapshot
            self._snapshots[input_path] = snapshot
        rehash = force_rehash or self.force_rehash
        if snapshot.hashed is not None and not rehash:
            return snapshot.hashed
        if cached is None and cached_by_path is not None:
            cached = cached_by_path.get(snapshot.path)
        if (
            cached is not None
            and cached.path == snapshot.path
            and cached.algorithm == "sha256"
            and not rehash
            and (cached.inode, cached.size, cached.mtime_ns)
            == (snapshot.inode, snapshot.size, snapshot.mtime_ns)
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
        if (before.st_ino, before.st_size, before.st_mtime_ns) == (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            return digest, after
        if attempt == retries:
            raise OSError(f"File changed while hashing: {path}")
    raise AssertionError("unreachable")


def _normalize_for_json(value: Any) -> JSON:
    if isinstance(value, BaseModel):
        return _normalize_for_json(value.model_dump(mode="json"))
    if isinstance(value, dict):
        invalid_type = next((type(key).__name__ for key in value if not isinstance(key, str)), None)
        if invalid_type is not None:
            raise TypeError(
                f"JSON objects used in varve keys must have string keys; got {invalid_type}"
            )
        return {key: _normalize_for_json(item) for key, item in value.items()}
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


def assert_no_symlink_path(path: Path, *, description: str) -> None:
    """Reject a symlink at any component of an explicitly supplied path."""

    absolute = path.expanduser().absolute()
    for component in reversed((absolute, *absolute.parents)):
        if component.is_symlink():
            raise ValueError(f"Symlinks are not supported in {description}: {component}")


def _tree_entries(
    root: Path,
    *,
    label: str,
    symlink_description: str,
    root_is_entry: bool = False,
) -> list[tuple[Path, str]]:
    if not root.exists():
        raise FileNotFoundError(f"{label} does not exist: {root}")
    if not (root.is_file() or root.is_dir()):
        suffix = " entry" if root_is_entry else ""
        raise ValueError(f"Unsupported {label.lower()}{suffix}: {root}")
    members = [root] if root.is_file() else [root, *sorted(root.rglob("*"))]
    result = []
    for member in members:
        if member.is_symlink():
            raise ValueError(f"Symlinks are not supported in {symlink_description}: {member}")
        if member.is_file() or member.is_dir():
            result.append((member, "file" if member.is_file() else "dir"))
        else:
            raise ValueError(f"Unsupported {label.lower()} entry: {member}")
    return result


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
        resolved_paths = resolve_paths(ctx)
        for path in [resolved_paths] if isinstance(resolved_paths, Path) else resolved_paths:
            assert_no_symlink_path(path, description="input dependencies")
            expanded = path.expanduser().resolve()
            for member, kind in _tree_entries(
                expanded, label="Input dependency", symlink_description="input dependencies"
            ):
                if kind == "file":
                    fingerprint = session.fingerprint(member, cached_by_path=cached_by_path)
                else:
                    stat = member.stat()
                    fingerprint = FileFingerprint(
                        path=str(member),
                        kind="dir",
                        inode=stat.st_ino,
                        size=0,
                        mtime_ns=stat.st_mtime_ns,
                        content_hash=json_sha256({"entry": "dir"}),
                    )
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
    cached_files = {
        entry.fingerprint.path: entry.fingerprint
        for entry in (() if cached is None else cached.manifest)
        if entry.fingerprint is not None
    }
    kind = "file" if resolved.is_file() else "dir"
    entries: list[ArtifactManifestEntry] = []
    tree = _tree_entries(
        resolved,
        label="Managed artifact",
        symlink_description="managed artifacts",
        root_is_entry=True,
    )
    for member, member_kind in tree:
        relative = member.relative_to(resolved).as_posix()
        if member_kind == "dir":
            entries.append(ArtifactManifestEntry(path=relative, kind="dir"))
        else:
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
