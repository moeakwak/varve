"""On-disk layout of pipeline metadata, managed artifacts, staging, and publish dirs.

```text
<root>/.varve/state.db | lock | locks/<branch>.lock | logs/<run_id>/<stage>/<unit>.log
<storage_root>/.internal/<branch>/<stage>/<unit_relpath>/
<storage_root>/.staging/<run_id>/<stage>/<unit_hash>/
<publish_root>/<branch>/            (persistent branch)
<publish_root>/.tmp/<branch>/       (temporary branch)
```
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import quote

DEFAULT_STORAGE = "default"
_PATH_SAFE = "-._~=+@"


def varve_dir(root: Path) -> Path:
    return root / ".varve"


def state_db_path(root: Path) -> Path:
    return varve_dir(root) / "state.db"


def pipeline_lock_path(root: Path) -> Path:
    return varve_dir(root) / "lock"


def branch_lock_path(root: Path, branch: str) -> Path:
    return varve_dir(root) / "locks" / f"{branch}.lock"


def logs_dir(root: Path, run_id: str | None = None) -> Path:
    base = varve_dir(root) / "logs"
    return base if run_id is None else base / run_id


def unit_log_path(root: Path, run_id: str, stage: str, unit_key: str) -> Path:
    name = path_segment(unit_key) if unit_key else "_"
    return logs_dir(root, run_id) / stage / f"{name}.log"


def path_segment(text: str) -> str:
    """Percent-encode ``text`` into one portable path segment."""

    return quote(text, safe=_PATH_SAFE)


def unit_key(pairs: Sequence[tuple[str, str]]) -> str:
    """``"a1=id1,a2=id2"`` in foreach order; ``""`` for a stage without axes."""

    return ",".join(f"{name}={item}" for name, item in pairs)


def unit_relpath(pairs: Sequence[tuple[str, str]]) -> str:
    """``"a1=id1/a2=id2"`` with unsafe characters percent-encoded; ``""`` without axes."""

    return "/".join(path_segment(f"{name}={item}") for name, item in pairs)


def unit_hash(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def internal_root(storage_root: Path, branch: str) -> Path:
    return storage_root / ".internal" / branch


def internal_dir(storage_root: Path, branch: str, stage: str, relpath: str) -> Path:
    base = internal_root(storage_root, branch) / stage
    return base / relpath if relpath else base


def internal_relpath(branch: str, stage: str, relpath: str) -> str:
    """Unit directory relative to its storage root (as stored in ``units``)."""

    base = f".internal/{branch}/{stage}"
    return f"{base}/{relpath}" if relpath else base


def staging_root(storage_root: Path, run_id: str | None = None) -> Path:
    base = storage_root / ".staging"
    return base if run_id is None else base / run_id


def staging_dir(storage_root: Path, run_id: str, stage: str, key: str) -> Path:
    return staging_root(storage_root, run_id) / stage / unit_hash(key)


def publish_dir(publish_root: Path, branch: str, *, temporary: bool) -> Path:
    return publish_root / ".tmp" / branch if temporary else publish_root / branch


def resolve_storage_roots(
    declared: Mapping[str, Path | str],
    root: Path,
    *,
    workspace: Mapping[str, Path | str] | None = None,
    pipeline_name: str | None = None,
) -> dict[str, Path]:
    """Combine storage roots: pipeline declarations > workspace ``[storage]`` > default.

    Relative paths resolve against ``root``. A workspace base maps to
    ``<base>/<pipeline_name>``. ``"default"`` falls back to ``<root>/out``.
    Paths are made absolute but symlinks are kept (prefix symlinks are allowed).
    """

    roots: dict[str, Path] = {}
    if workspace:
        if pipeline_name is None:
            raise ValueError("workspace storage roots need the pipeline name")
        for name, base in workspace.items():
            roots[name] = _absolute(root, Path(base) / pipeline_name)
    for name, path in declared.items():
        roots[name] = _absolute(root, Path(path))
    roots.setdefault(DEFAULT_STORAGE, _absolute(root, Path("out")))
    return roots


def resolve_publish_root(
    declared: Path | str | None, storage_roots: Mapping[str, Path], root: Path
) -> Path:
    """``Pipeline.publish_root`` (relative to ``root``) or the default storage root."""

    if declared is None:
        return storage_roots[DEFAULT_STORAGE]
    return _absolute(root, declared)


def _absolute(root: Path, path: Path | str) -> Path:
    path = Path(path).expanduser()
    return path if path.is_absolute() else (root / path).absolute()
