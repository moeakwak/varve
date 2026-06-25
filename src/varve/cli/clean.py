"""Destructive clean operations with conservative safety checks."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from varve.experiment import Experiment
from varve.models import Manifest
from varve.store.lock import OutputLock
from varve.store.store import Store


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True


def _validate_destructive(root: Path, allowed_roots: list[Path] | None = None) -> None:
    resolved = root.expanduser().resolve()
    if str(root) == "":
        raise ValueError("Refusing to clean an empty path")
    dangerous = {Path("/").resolve(), Path.home().resolve(), Path.cwd().resolve()}
    if resolved in dangerous:
        raise ValueError(f"Refusing to clean dangerous path: {resolved}")
    if allowed_roots is not None and not any(_is_relative_to(resolved, allowed) for allowed in allowed_roots):
        raise ValueError(f"Refusing to clean path outside allowed roots: {resolved}")


def _confirm(message: str, yes: bool, confirm: Callable[[str], bool] | None) -> None:
    if yes:
        return
    if confirm is not None and confirm(message):
        return
    raise ValueError("Clean requires confirmation or yes=True")


def _read_manifest_anchor(store: Store, experiment: type[Experiment]) -> Manifest:
    manifest_path = store.root / "manifest.json"
    if not manifest_path.exists():
        raise ValueError(f"Missing varve manifest anchor: {manifest_path}")
    manifest = Manifest.model_validate_json(manifest_path.read_text(encoding="utf-8"))
    if manifest.experiment != experiment.__name__:
        raise ValueError(
            f"Varve manifest belongs to {manifest.experiment}, not {experiment.__name__}"
        )
    return manifest


def _downstream_closure(experiment: type[Experiment], target: str) -> set[str]:
    stages = experiment.stages()
    descendants = {name: set() for name in stages}
    for name, spec in stages.items():
        for upstream in spec.needs:
            descendants[upstream].add(name)
    seen: set[str] = set()
    stack = [target]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(descendants[name])
    return seen


def _record_paths(record) -> list[str]:
    if record.kind == "single":
        assert record.produces is not None
        return [item.path for item in record.produces]
    assert record.outputs is not None
    return [item.path for item in record.outputs]


def _collect_target_records(store: Store, stages: set[str]) -> dict[str, Any]:
    records = {}
    for stage_name in stages:
        record = store.read_success(stage_name)
        if record is not None:
            records[stage_name] = record
    return records


def _validate_record_paths(root: Path, records: dict[str, Any]) -> None:
    outside = []
    for record in records.values():
        for relative in _record_paths(record):
            path = root / relative
            if not _is_relative_to(path, root):
                outside.append(path)
    if outside:
        listed = ", ".join(str(path) for path in outside)
        raise ValueError(f"Refusing to clean output outside root: {listed}")


def clean(
    experiment: type[Experiment],
    config: Any,
    *,
    cli_out: Path | None = None,
    branch: str = "main",
    is_temporary: bool = False,
    target: str | None = None,
    yes: bool = False,
    allowed_roots: list[Path] | None = None,
    confirm: Callable[[str], bool] | None = None,
) -> None:
    root = experiment.output_root(
        config,
        cli_out=cli_out,
        branch=branch,
        is_temporary=is_temporary,
    )
    store = Store(root)
    with OutputLock(store.root):
        _read_manifest_anchor(store, experiment)

        if target is None:
            _validate_destructive(root, allowed_roots)
            _confirm(f"Clean full varve output root {root}?", yes, confirm)
            shutil.rmtree(root)
            return

        if target not in experiment.stages():
            raise ValueError(f"Unknown varve stage: {target}")
        stage_names = _downstream_closure(experiment, target)
        records = _collect_target_records(store, stage_names)
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
