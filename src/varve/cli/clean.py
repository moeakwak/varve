"""Destructive clean operations with conservative safety checks."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from varve.engine.runner import selected_stages
from varve.matrix import PipelineGraph, build_graph
from varve.models import Manifest
from varve.pipeline import Pipeline
from varve.store.lock import OutputLock
from varve.store.store import Store


def _validate_destructive(root: Path, allowed_roots: list[Path] | None = None) -> None:
    resolved = root.expanduser().resolve()
    if str(root) == "":
        raise ValueError("Refusing to clean an empty path")
    dangerous = {Path("/").resolve(), Path.home().resolve(), Path.cwd().resolve()}
    if resolved in dangerous:
        raise ValueError(f"Refusing to clean dangerous path: {resolved}")
    if allowed_roots is not None and not any(
        resolved.is_relative_to(allowed.expanduser().resolve()) for allowed in allowed_roots
    ):
        raise ValueError(f"Refusing to clean path outside allowed roots: {resolved}")


def _confirm(message: str, yes: bool, confirm: Callable[[str], bool] | None) -> None:
    if yes:
        return
    if confirm is not None and confirm(message):
        return
    raise ValueError("Clean requires confirmation or yes=True")


def _read_manifest_anchor(store: Store, pipeline: type[Pipeline]) -> Manifest:
    manifest_path = store.root / "manifest.json"
    manifest = store.read_manifest()
    if manifest is None:
        raise ValueError(f"Missing varve manifest anchor: {manifest_path}")
    if manifest.pipeline != pipeline.__name__:
        raise ValueError(f"Varve manifest belongs to {manifest.pipeline}, not {pipeline.__name__}")
    return manifest


def _record_paths(record) -> list[str]:
    if record.kind == "single":
        assert record.produces is not None
        return [item.path for item in record.produces]
    assert record.outputs is not None
    return [item.path for item in record.outputs]


def _validate_record_paths(root: Path, records: dict[str, Any]) -> None:
    outside = []
    resolved_root = root.resolve()
    for record in records.values():
        for relative in _record_paths(record):
            path = root / relative
            if not path.resolve().is_relative_to(resolved_root):
                outside.append(path)
    if outside:
        listed = ", ".join(str(path) for path in outside)
        raise ValueError(f"Refusing to clean output outside root: {listed}")


def clean(
    pipeline: type[Pipeline],
    config: Any,
    *,
    cli_out: Path | None = None,
    branch: str = "main",
    is_temporary: bool = False,
    target: str | None = None,
    yes: bool = False,
    allowed_roots: list[Path] | None = None,
    confirm: Callable[[str], bool] | None = None,
    axes: dict[str, tuple[str, ...]] | None = None,
    graph: PipelineGraph | None = None,
) -> None:
    root = pipeline.output_root(
        config,
        cli_out=cli_out,
        branch=branch,
        is_temporary=is_temporary,
    )
    store = Store(root)
    with OutputLock(store.root):
        _read_manifest_anchor(store, pipeline)

        if target is None:
            _validate_destructive(root, allowed_roots)
            _confirm(f"Clean full varve output root {root}?", yes, confirm)
            shutil.rmtree(root)
            return

        stage_names = selected_stages(graph or build_graph(pipeline, axes), downstream=target)
        records = {}
        for stage_name in stage_names:
            record = store.read_success(stage_name)
            if record is not None:
                records[stage_name] = record
        _validate_record_paths(root, records)
        _confirm(f"Clean varve stage subtree {target}?", yes, confirm)

        for stage_name, record in records.items():
            for relative in _record_paths(record):
                path = root / relative
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)
        for stage_name in stage_names:
            (store.root / "stages" / f"{stage_name}.json").unlink(missing_ok=True)
            store.clear_attempt(stage_name)
            store.clear_partial(stage_name)
            store.clear_review(stage_name)
            store.clear_failure(stage_name)
