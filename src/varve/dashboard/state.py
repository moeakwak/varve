"""Load read-only dashboard state using the engine state evaluator."""

from __future__ import annotations

import importlib
from datetime import datetime
from pathlib import Path

from varve.branch_config import ResolvedBranch, resolve_branch
from varve.dashboard.models import (
    ArtifactState,
    ErrorPhase,
    ExperimentEntry,
    ExperimentState,
    StageState,
    StateError,
)
from varve.engine.runner import evaluate_state
from varve.engine.state import Status
from varve.experiment import Experiment
from varve.models import SuccessRecord
from varve.store.store import Store

STATUS_PRIORITY: tuple[Status, ...] = (
    "hit",
    "artifact-missing",
    "resume",
    "no-cache",
    "stale",
    "dirty",
    "corrupt-store",
    "unrecoverable",
)
_STATUS_PRIORITY = {status: index for index, status in enumerate(STATUS_PRIORITY)}


def load_state(entry: ExperimentEntry) -> ExperimentState:
    """Load one experiment branch's current cache state."""
    if entry.manifest_error:
        return _error(entry, "manifest", entry.manifest_error)
    if entry.experiment_name is None:
        return _error(entry, "manifest", "Manifest is missing experiment")
    if entry.module is None:
        return _error(entry, "manifest", "Manifest is missing module")

    try:
        experiment = import_entry_experiment(entry)
    except Exception as error:  # noqa: BLE001 - dashboard must keep scanning after import failures.
        return _error(entry, "import", str(error))

    try:
        resolved = resolve_entry_branch(entry, experiment)
    except Exception as error:  # noqa: BLE001 - dashboard reports resolver diagnostics.
        return _error(entry, "resolve", str(error))

    try:
        outcomes = evaluate_state(
            experiment,
            resolved.config,
            args=experiment.Args(),
            cli_out=resolved.output_base,
            branch=resolved.branch,
            is_temporary=resolved.is_temporary,
        )
    except Exception as error:  # noqa: BLE001 - dashboard reports evaluator diagnostics.
        return _error(entry, "evaluate", str(error))

    outcomes_by_stage = {outcome.stage: outcome for outcome in outcomes}
    store = Store(entry.output_root)
    stages: list[StageState] = []
    for name in experiment.topo_order():
        outcome = outcomes_by_stage[name]
        success = store.read_success(name)
        stages.append(
            StageState(
                name=name,
                status=outcome.status,
                reason=outcome.reason,
                artifacts=_artifacts(entry, success) if success is not None else [],
                committed_at=_parse_datetime(success.committed_at) if success is not None else None,
                upstreams=list(experiment.stages()[name].needs),
            )
        )

    return ExperimentState(
        entry=entry,
        stages=stages,
        status=_aggregate_status(stages),
        error=None,
    )


def import_entry_experiment(entry: ExperimentEntry) -> type[Experiment]:
    if entry.manifest_error:
        raise ValueError(entry.manifest_error)
    if entry.experiment_name is None:
        raise ValueError("Manifest is missing experiment")
    if entry.module is None:
        raise ValueError("Manifest is missing module")
    return _import_experiment(entry.module, entry.experiment_name)


def resolve_entry_branch(
    entry: ExperimentEntry,
    experiment: type[Experiment],
) -> ResolvedBranch:
    return resolve_branch(
        experiment,
        branch=entry.branch,
        override_json=None,
        cli_out=_output_base(entry),
    )


def _import_experiment(module_name: str, class_name: str) -> type[Experiment]:
    module = importlib.import_module(module_name)
    value = getattr(module, class_name)
    if not isinstance(value, type) or not issubclass(value, Experiment):
        raise TypeError(f"{module_name}.{class_name} is not a varve Experiment")
    return value


def _output_base(entry: ExperimentEntry) -> Path:
    if entry.output_root.parent.name == ".tmp":
        return entry.output_root.parent.parent
    return entry.output_root.parent


def _artifacts(entry: ExperimentEntry, success: SuccessRecord) -> list[ArtifactState]:
    if success.kind == "single":
        assert success.produces is not None
        paths = [Path(produced.path) for produced in success.produces]
    else:
        assert success.outputs is not None
        paths = [Path(output.path) for output in success.outputs]
    return [
        ArtifactState(path=path, exists=(entry.output_root / path).exists())
        for path in paths
    ]


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
    entry: ExperimentEntry,
    phase: ErrorPhase,
    message: str,
) -> ExperimentState:
    return ExperimentState(
        entry=entry,
        stages=[],
        status="error",
        error=StateError(phase=phase, message=message),
    )
