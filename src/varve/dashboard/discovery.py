"""Discover varve stores under a scan root without importing pipelines."""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import ValidationError

from varve.dashboard.models import PipelineEntry
from varve.models import Manifest


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
        if _is_temporary_output_root(output_root) and not include_temporary:
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
                manifest_error=manifest_error,
                temporary=_is_temporary_output_root(output_root),
            )
        )
    return sort_entries(entries)


def sort_entries(entries: list[PipelineEntry]) -> list[PipelineEntry]:
    """Sort entries by their manifest identities rather than path-derived ids."""

    return sorted(
        entries,
        key=lambda entry: (
            entry.module or "",
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
            if (
                include_temporary
                or not (entry.temporary or entry.output_root.parent.name == ".tmp")
            )
            and (branch is None or entry.branch == branch)
            and (prefix is None or (entry.module or "").startswith(prefix))
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


def _is_temporary_output_root(output_root: Path) -> bool:
    return output_root.parent.name == ".tmp" and output_root.parent.parent.name == "out"


def _read_manifest(manifest_path: Path) -> tuple[Manifest | None, str | None]:
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return Manifest.model_validate(data), None
    except (json.JSONDecodeError, OSError, ValidationError) as error:
        return None, str(error)
