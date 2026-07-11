"""File fingerprints and canonical JSON helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from varve.keyspec import JSON
from varve.models import FileFingerprint


@dataclass(slots=True)
class _FileSnapshot:
    path: str
    size: int
    mtime: float
    hashed: FileFingerprint | None = None


@dataclass
class FingerprintSession:
    """Reuse filesystem observations within one probe or run command."""

    _snapshots: dict[str, _FileSnapshot] = field(default_factory=dict)

    def fingerprint(
        self,
        path: Path,
        cached: FileFingerprint | None = None,
        *,
        cached_by_path: Mapping[str, FileFingerprint] | None = None,
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
                    size=stat.st_size,
                    mtime=stat.st_mtime,
                )
                self._snapshots[normalized] = snapshot
            self._snapshots[input_path] = snapshot
        if snapshot.hashed is not None:
            return snapshot.hashed
        if cached is None and cached_by_path is not None:
            cached = cached_by_path.get(snapshot.path)
        if (
            cached is not None
            and cached.path == snapshot.path
            and cached.size == snapshot.size
            and cached.mtime == snapshot.mtime
        ):
            return cached

        normalized_path = Path(snapshot.path)
        result = FileFingerprint(
            path=snapshot.path,
            size=snapshot.size,
            mtime=snapshot.mtime,
            sha256=_sha256_file(normalized_path),
        )
        snapshot.hashed = result
        return result


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return f"sha256:{hasher.hexdigest()}"


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
    """Project file fingerprints onto the digests used by content keys."""

    return {
        name: json_sha256(sorted(member.sha256 for member in members))
        for name, members in sorted(files.items())
    }


def file_fingerprint(
    path: Path,
    cached: FileFingerprint | None = None,
    *,
    session: FingerprintSession | None = None,
) -> FileFingerprint:
    return (session or FingerprintSession()).fingerprint(path, cached)


def _coerce_paths(value: Path | list[Path]) -> list[Path]:
    if isinstance(value, Path):
        return [value]
    return list(value)


def files_fingerprints(
    ctx: Any,
    files_spec: Mapping[str, Callable[[Any], Path | list[Path]]],
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
            fingerprint = session.fingerprint(path, cached_by_path=cached_by_path)
            members[fingerprint.path] = fingerprint
        results[name] = [members[path] for path in sorted(members)]
    return results
