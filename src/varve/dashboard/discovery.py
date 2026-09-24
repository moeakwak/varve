"""Discover varve stores under a scan root without importing pipelines."""

from __future__ import annotations

import json
import os
import sys
from importlib.machinery import PathFinder
from pathlib import Path

from pydantic import ValidationError

from varve.branch import load_branches
from varve.dashboard.models import PipelineEntry
from varve.models import Manifest


def is_manual_entry(entry: PipelineEntry) -> bool:
    """Read branch policy next to the persisted module without importing it or its parents."""
    if entry.module is None or entry.manifest_error is not None:
        return False
    search_path = None
    parts = entry.module.split(".")
    module_file = None
    for index in range(len(parts)):
        name = ".".join(parts[: index + 1])
        loaded = sys.modules.get(name)
        if loaded is not None:
            module_file = getattr(loaded, "__file__", None)
            search_path = getattr(loaded, "__path__", None)
        else:
            # Use the leaf name with the explicit parent path. A dotted namespace
            # spec otherwise asks sys.modules for a parent we deliberately did not import.
            spec = PathFinder.find_spec(parts[index], search_path)
            if spec is None:
                return False  # Normal state loading reports an unavailable import target.
            module_file = spec.origin
            search_path = (
                list(spec.submodule_search_locations)
                if spec.submodule_search_locations is not None
                else None
            )
        if index < len(parts) - 1 and search_path is None:
            return False
    if module_file is None:
        return False
    definition = load_branches(Path(module_file).resolve().parent / "varve.yaml").get(entry.branch)
    return definition.manual if definition is not None else False


def discover_pipelines(root: Path, *, include_temporary: bool = False) -> list[PipelineEntry]:
    """Return all discovered varve stores under root without importing pipelines."""
    root = Path(root).resolve()
    if not root.exists():
        return []

    entries: list[PipelineEntry] = []
    for current, directories, _files in os.walk(root):
        if ".varve" not in directories:
            continue
        directories.remove(".varve")
        output_root = Path(current)
        store_root = output_root / ".varve"
        manifest_path = store_root / "manifest.json"
        if not manifest_path.exists():
            continue
        split = _branch_output_id(root, output_root)
        if split is None:
            continue
        directories.clear()
        temporary = output_root.parent.name == ".tmp" and output_root.parent.parent.name == "out"
        if temporary and not include_temporary:
            continue
        pipeline_id, branch = split
        manifest, manifest_error = _read_manifest(manifest_path)
        entries.append(
            PipelineEntry(
                output_root=output_root,
                pipeline_id=pipeline_id,
                pipeline_name=manifest.pipeline if manifest is not None else None,
                branch=branch,
                module=manifest.module if manifest is not None else None,
                name=manifest.name if manifest is not None else None,
                manifest_error=manifest_error,
            )
        )
    return sort_entries(entries)


def sort_entries(entries: list[PipelineEntry]) -> list[PipelineEntry]:
    """Sort entries by their manifest identities rather than path-derived ids."""

    return sorted(
        entries,
        key=lambda entry: (
            entry.selector,
            entry.branch,
            entry.pipeline_name or "",
            str(entry.output_root),
        ),
    )


def filter_entries(
    entries: list[PipelineEntry],
    *,
    prefix: str | None = None,
    branch: str | None = None,
    include_temporary: bool = False,
) -> list[PipelineEntry]:
    """Apply the discovery scope shared by overview and bulk commands."""

    return sort_entries(
        [
            entry
            for entry in entries
            if (include_temporary or entry.output_root.parent.name != ".tmp")
            and (branch is None or entry.branch == branch)
            and (prefix is None or entry.selector.startswith(prefix))
        ]
    )


def _branch_output_id(root: Path, output_root: Path) -> tuple[str, str] | None:
    if output_root.parent.name == ".tmp" and output_root.parent.parent.name == "out":
        pipeline_root = output_root.parent.parent.parent
    elif output_root.parent.name == "out":
        pipeline_root = output_root.parent.parent
    else:
        return None
    try:
        relative = pipeline_root.relative_to(root)
    except ValueError:
        relative = Path(pipeline_root.name)
    pipeline_id = ".".join(relative.parts) if relative.parts else pipeline_root.name
    return pipeline_id, output_root.name


def _read_manifest(manifest_path: Path) -> tuple[Manifest | None, str | None]:
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return Manifest.model_validate(data), None
    except (json.JSONDecodeError, OSError, ValidationError) as error:
        return None, str(error)
