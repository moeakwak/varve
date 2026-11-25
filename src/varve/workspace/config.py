"""``varve.toml`` workspace settings and the ``VARVE_ROOT_BASE`` relocation knob.

``varve.toml`` is purely declarative and parsed without importing any user
module::

    [workspace]
    pipelines = ["studies.exp.dataset_audit.label_renderability", "pkg.mod:Pipeline"]

    [storage]
    interim = "/data_disk/varve/interim"   # relative paths resolve against varve.toml's dir

    [defaults]
    worker_env = { OMP_NUM_THREADS = "1" }

``VARVE_ROOT_BASE=/tmp/x`` relocates every pipeline that is not given an
explicit ``--root``: its root becomes ``/tmp/x/<pipeline_name>`` and every
storage root (and the publish root) is placed below that root, ignoring
``storage_roots()``, ``publish_root()`` and ``[storage]``. Ref discovery looks
for upstream stores under the same base, so whole chains can run in a
scratch directory without touching real outputs.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised on Python 3.10 only
    import tomli as tomllib  # pyright: ignore[reportMissingImports]

CONFIG_NAME = "varve.toml"
ROOT_BASE_ENV = "VARVE_ROOT_BASE"


@dataclass(frozen=True)
class WorkspaceSettings:
    """Workspace-level settings the engine receives (from ``varve.toml`` and the env).

    ``storage`` maps storage names to base dirs (``<base>/<pipeline_name>``
    unless the pipeline declares that storage itself); ``worker_env`` is
    applied to process-pool workers; ``pipeline_name`` names the pipeline's
    subdirectory under workspace storage bases and ``root_base`` (default: its
    selector). ``root_base`` relocates roots and storages (``VARVE_ROOT_BASE``).
    ``config_path`` is the ``varve.toml`` read (if any), ``workspace_root`` the
    directory store discovery scans, and ``pipelines`` the registered entries.
    """

    storage: Mapping[str, Path] = field(default_factory=dict)
    worker_env: Mapping[str, str] = field(default_factory=dict)
    pipeline_name: str | None = None
    root_base: Path | None = None
    config_path: Path | None = None
    workspace_root: Path | None = None
    pipelines: tuple[str, ...] = ()

    @property
    def scan_root(self) -> Path:
        """Where stores are discovered: the relocation base, else the workspace root, else cwd."""

        if self.root_base is not None:
            return self.root_base
        return self.workspace_root or Path.cwd()


class WorkspaceConfigError(ValueError):
    """``varve.toml`` is malformed."""


def find_config(start: Path) -> Path | None:
    """The nearest ``varve.toml`` in ``start`` or one of its parents."""

    current = Path(start).absolute()
    for directory in (current, *current.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def root_base_from_env(environ: Mapping[str, str] | None = None) -> Path | None:
    value = (environ if environ is not None else os.environ).get(ROOT_BASE_ENV, "").strip()
    return Path(value).expanduser().absolute() if value else None


def load_workspace(
    starts: Sequence[Path] | None = None, *, environ: Mapping[str, str] | None = None
) -> WorkspaceSettings:
    """Settings from the first ``varve.toml`` found walking up from ``starts``
    (default: the cwd), plus ``VARVE_ROOT_BASE``."""

    root_base = root_base_from_env(environ)
    for start in starts if starts is not None else (Path.cwd(),):
        path = find_config(start)
        if path is not None:
            return parse_config(path, root_base=root_base)
    return WorkspaceSettings(root_base=root_base)


def settings_for(pipeline: Any) -> WorkspaceSettings:
    """Default settings of a single-pipeline command: ``varve.toml`` walking up from
    the cwd, else from the pipeline module's directory."""

    starts = [Path.cwd()]
    try:
        starts.append(pipeline.module_file().parent)
    except ValueError:
        pass
    return load_workspace(starts)


def parse_config(path: Path, *, root_base: Path | None = None) -> WorkspaceSettings:
    path = Path(path).absolute()
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise WorkspaceConfigError(f"{path}: {error}") from error
    unknown = set(data) - {"workspace", "storage", "defaults"}
    if unknown:
        raise WorkspaceConfigError(
            f"{path}: unknown tables {sorted(unknown)}; expected workspace, storage, defaults"
        )
    base = path.parent

    workspace = _table(data, "workspace", path)
    _check_keys(workspace, {"pipelines"}, "workspace", path)
    pipelines = workspace.get("pipelines", [])
    if not isinstance(pipelines, list) or not all(
        isinstance(item, str) and _valid_entry(item) for item in pipelines
    ):
        raise WorkspaceConfigError(
            f"{path}: [workspace] pipelines must be a list of module names "
            "('pkg.mod' or 'pkg.mod:Class')"
        )
    if len(set(pipelines)) != len(pipelines):
        raise WorkspaceConfigError(f"{path}: [workspace] pipelines has duplicates")

    storage: dict[str, Path] = {}
    for name, value in _table(data, "storage", path).items():
        if not isinstance(value, str) or not value:
            raise WorkspaceConfigError(f"{path}: [storage] {name} must be a path string")
        target = Path(value).expanduser()
        storage[name] = target if target.is_absolute() else (base / target).absolute()

    defaults = _table(data, "defaults", path)
    _check_keys(defaults, {"worker_env"}, "defaults", path)
    worker_env = defaults.get("worker_env", {})
    if not isinstance(worker_env, dict) or not all(
        isinstance(value, str) for value in worker_env.values()
    ):
        raise WorkspaceConfigError(f"{path}: [defaults] worker_env must map names to string values")

    return WorkspaceSettings(
        storage=storage,
        worker_env=dict(worker_env),
        root_base=root_base,
        config_path=path,
        workspace_root=base,
        pipelines=tuple(pipelines),
    )


def _table(data: Mapping[str, Any], name: str, path: Path) -> dict[str, Any]:
    table = data.get(name, {})
    if not isinstance(table, dict):
        raise WorkspaceConfigError(f"{path}: [{name}] must be a table")
    return table


def _check_keys(table: Mapping[str, Any], allowed: set[str], name: str, path: Path) -> None:
    unknown = set(table) - allowed
    if unknown:
        raise WorkspaceConfigError(f"{path}: unknown keys in [{name}]: {sorted(unknown)}")


def _valid_entry(entry: str) -> bool:
    module, _, qualname = entry.partition(":")
    parts = [*module.split("."), *(qualname.split(".") if qualname else [])]
    return all(part.isidentifier() for part in parts) and (":" not in entry or bool(qualname))
