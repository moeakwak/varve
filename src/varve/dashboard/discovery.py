"""Discover varve stores under a scan root without importing pipelines."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import ValidationError

from varve.dashboard.models import PipelineEntry
from varve.models import Manifest


def discover_pipelines(root: Path, *, include_temporary: bool = False) -> list[PipelineEntry]:
    """Return all discovered varve stores under root, sorted by stable id."""
    root = Path(root).resolve()
    if not root.exists():
        return []

    entries: list[PipelineEntry] = []
    for store_root in root.rglob(".varve"):
        if not store_root.is_dir():
            continue
        manifest_path = store_root / "manifest.json"
        if not manifest_path.exists():
            continue
        output_root = store_root.parent
        if _is_temporary_output_root(output_root) and not include_temporary:
            continue
        split = _branch_output_id(root, output_root)
        if split is None:
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
            )
        )
    return sorted(entries, key=lambda entry: (entry.pipeline_id, entry.branch))


def _relative_parts(root: Path, output_root: Path) -> tuple[str, ...]:
    try:
        relative = output_root.relative_to(root)
    except ValueError:
        relative = output_root
    return relative.parts


def _branch_output_id(root: Path, output_root: Path) -> tuple[str, str] | None:
    parts = _relative_parts(root, output_root)
    if len(parts) >= 3 and parts[-3] == "out" and parts[-2] == ".tmp":
        pipeline_parts = parts[:-3]
        pipeline_id = (
            ".".join(pipeline_parts) if pipeline_parts else output_root.parent.parent.parent.name
        )
        return pipeline_id, parts[-1]
    if output_root.parent.name == ".tmp" and output_root.parent.parent.name == "out":
        return output_root.parent.parent.parent.name, output_root.name
    if len(parts) >= 2 and parts[-2] == "out":
        pipeline_parts = parts[:-2]
        pipeline_id = ".".join(pipeline_parts) if pipeline_parts else output_root.parent.parent.name
        return pipeline_id, parts[-1]
    if output_root.parent.name == "out":
        return output_root.parent.parent.name, output_root.name
    return None


def _is_temporary_output_root(output_root: Path) -> bool:
    parts = output_root.resolve().parts
    return any(left == "out" and right == ".tmp" for left, right in zip(parts, parts[1:]))


def _read_manifest(manifest_path: Path) -> tuple[Manifest | None, str | None]:
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        return Manifest.model_validate(data), None
    except (json.JSONDecodeError, OSError, ValidationError) as error:
        return None, str(error)
