"""Discover varve stores under a scan root without importing experiments."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from varve.dashboard.models import ExperimentEntry
from varve.models import Manifest


def discover_experiments(root: Path, *, include_temporary: bool = False) -> list[ExperimentEntry]:
    """Return all discovered varve stores under root, sorted by stable id."""
    root = Path(root).resolve()
    if not root.exists():
        return []

    entries: list[ExperimentEntry] = []
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
        experiment_id, branch = split
        manifest, manifest_error = _read_manifest(manifest_path)
        entries.append(
            ExperimentEntry(
                output_root=output_root,
                experiment_id=experiment_id,
                experiment_name=manifest.experiment if manifest is not None else None,
                branch=branch,
                module=manifest.module if manifest is not None else None,
                manifest_error=manifest_error,
            )
        )
    return sorted(entries, key=lambda entry: (entry.experiment_id, entry.branch))


def _relative_parts(root: Path, output_root: Path) -> tuple[str, ...]:
    try:
        relative = output_root.relative_to(root)
    except ValueError:
        relative = output_root
    return relative.parts


def _branch_output_id(root: Path, output_root: Path) -> tuple[str, str] | None:
    parts = _relative_parts(root, output_root)
    if len(parts) >= 3 and parts[-3] == "out" and parts[-2] == ".tmp":
        experiment_parts = parts[:-3]
        experiment_id = (
            ".".join(experiment_parts)
            if experiment_parts
            else output_root.parent.parent.parent.name
        )
        return experiment_id, parts[-1]
    if output_root.parent.name == ".tmp" and output_root.parent.parent.name == "out":
        return output_root.parent.parent.parent.name, output_root.name
    if len(parts) >= 2 and parts[-2] == "out":
        experiment_parts = parts[:-2]
        experiment_id = ".".join(experiment_parts) if experiment_parts else output_root.parent.parent.name
        return experiment_id, parts[-1]
    if output_root.parent.name == "out":
        return output_root.parent.parent.name, output_root.name
    return None


def _is_temporary_output_root(output_root: Path) -> bool:
    parts = output_root.resolve().parts
    return any(left == "out" and right == ".tmp" for left, right in zip(parts, parts[1:]))


def _read_manifest(manifest_path: Path) -> tuple[Manifest | None, str | None]:
    try:
        data: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
        return Manifest.model_validate(data), None
    except (json.JSONDecodeError, OSError, ValidationError) as error:
        return None, str(error)
