"""Load read-only dashboard state using the engine state evaluator."""

from __future__ import annotations

import importlib
from datetime import datetime
from pathlib import Path

from varve.branch_config import ResolvedBranch, resolve_branch
from varve.dashboard.models import (
    ArtifactState,
    ErrorPhase,
    PipelineEntry,
    PipelineState,
    StageState,
    StateError,
)
from varve.engine.runner import evaluate_state
from varve.engine.state import Status
from varve.matrix import build_graph
from varve.models import SuccessRecord
from varve.pipeline import Pipeline
from varve.store.store import Store

STATUS_PRIORITY: tuple[Status, ...] = (
    "hit",
    "artifact-missing",
    "resume",
    "no-cache",
    "stale",
    "dirty",
)
_STATUS_PRIORITY = {status: index for index, status in enumerate(STATUS_PRIORITY)}


def load_state(entry: PipelineEntry) -> PipelineState:
    """Load one pipeline branch's current cache state."""
    if entry.manifest_error:
        return _error(entry, "manifest", entry.manifest_error)
    if entry.pipeline_name is None:
        return _error(entry, "manifest", "Manifest is missing pipeline")
    if entry.module is None:
        return _error(entry, "manifest", "Manifest is missing module")

    try:
        pipeline = import_entry_pipeline(entry)
    except Exception as error:  # noqa: BLE001 - dashboard must keep scanning after import failures.
        return _error(entry, "import", str(error))

    try:
        resolved = resolve_entry_branch(entry, pipeline)
    except Exception as error:  # noqa: BLE001 - dashboard reports resolver diagnostics.
        return _error(entry, "resolve", str(error))

    try:
        graph = build_graph(pipeline, resolved.axes)
        outcomes = evaluate_state(
            pipeline,
            resolved.config,
            args=pipeline.Args(),
            cli_out=resolved.output_base,
            branch=resolved.branch,
            is_temporary=resolved.is_temporary,
            axes=resolved.axes,
            graph=graph,
        )
    except Exception as error:  # noqa: BLE001 - dashboard reports evaluator diagnostics.
        return _error(entry, "evaluate", str(error))

    outcomes_by_stage = {outcome.stage: outcome for outcome in outcomes}
    store = Store(entry.output_root)
    stages: list[StageState] = []
    for name in graph.topo_order():
        outcome = outcomes_by_stage[name]
        success = store.read_success(name)
        stages.append(
            StageState(
                name=name,
                status=outcome.status,
                reason=outcome.reason,
                artifacts=_artifacts(entry, success) if success is not None else [],
                committed_at=_parse_datetime(success.committed_at) if success is not None else None,
                elapsed=success.elapsed if success is not None else None,
                upstreams=list(graph.stages[name].needs),
            )
        )

    return PipelineState(
        entry=entry,
        stages=stages,
        status=_aggregate_status(stages),
        error=None,
    )


def import_entry_pipeline(entry: PipelineEntry) -> type[Pipeline]:
    if entry.manifest_error:
        raise ValueError(entry.manifest_error)
    if entry.pipeline_name is None:
        raise ValueError("Manifest is missing pipeline")
    if entry.module is None:
        raise ValueError("Manifest is missing module")
    return _import_pipeline(entry.module, entry.pipeline_name)


def resolve_entry_branch(
    entry: PipelineEntry,
    pipeline: type[Pipeline],
) -> ResolvedBranch:
    return resolve_branch(
        pipeline,
        branch=entry.branch,
        override_json=None,
        cli_out=_output_base(entry),
    )


def _import_pipeline(module_name: str, class_name: str) -> type[Pipeline]:
    module = importlib.import_module(module_name)
    value = getattr(module, class_name)
    if not isinstance(value, type) or not issubclass(value, Pipeline):
        raise TypeError(f"{module_name}.{class_name} is not a varve Pipeline")
    return value


def _output_base(entry: PipelineEntry) -> Path:
    if entry.output_root.parent.name == ".tmp":
        return entry.output_root.parent.parent
    return entry.output_root.parent


def _artifacts(entry: PipelineEntry, success: SuccessRecord) -> list[ArtifactState]:
    if success.kind == "single":
        assert success.produces is not None
        paths = [Path(produced.path) for produced in success.produces]
    else:
        assert success.outputs is not None
        paths = [Path(output.path) for output in success.outputs]
    return [ArtifactState(path=path, exists=(entry.output_root / path).exists()) for path in paths]


def _parse_datetime(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _aggregate_status(stages: list[StageState]) -> Status:
    if not stages:
        return "hit"
    return max(stages, key=lambda stage: _STATUS_PRIORITY[stage.status]).status


def _error(
    entry: PipelineEntry,
    phase: ErrorPhase,
    message: str,
) -> PipelineState:
    return PipelineState(
        entry=entry,
        stages=[],
        status="error",
        error=StateError(phase=phase, message=message),
    )
