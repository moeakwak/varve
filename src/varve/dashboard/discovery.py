"""Discover varve stores under a scan root without importing experiments."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from varve.dashboard.models import ExperimentEntry
from varve.models import Manifest


def discover_experiments(root: Path) -> list[ExperimentEntry]:
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
        entries.append(
            ExperimentEntry(
                output_root=output_root,
                experiment_id=_experiment_id(root, output_root),
                experiment_name=_read_experiment_name(manifest_path),
            )
        )
    return sorted(entries, key=lambda entry: entry.experiment_id)


def _experiment_id(root: Path, output_root: Path) -> str:
    try:
        relative = output_root.relative_to(root)
    except ValueError:
        relative = output_root
    if not relative.parts:
        return output_root.name
    return ".".join(relative.parts)


def _read_experiment_name(manifest_path: Path) -> str | None:
    try:
        data: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
        return Manifest.model_validate(data).experiment
    except (json.JSONDecodeError, OSError, ValidationError):
        return None
