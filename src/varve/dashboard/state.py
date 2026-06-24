"""Load read-only dashboard state from a varve store."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from varve.dashboard.models import (
    ArtifactState,
    ExperimentEntry,
    ExperimentState,
    OverallStatus,
    StageState,
    StageStatus,
)
from varve.models import SuccessRecord
from varve.store.store import CorruptStore, Store

_STATUS_PRIORITY: dict[StageStatus, int] = {
    "ok": 0,
    "artifact-missing": 1,
    "interrupted": 2,
    "corrupt": 3,
}


def load_state(entry: ExperimentEntry) -> ExperimentState:
    """Load one experiment's dashboard state from its latest store snapshot."""
    store = Store(entry.output_root)
    stages: list[StageState] = []
    for name in _stage_names(store):
        stage = _load_stage(entry, store, name)
        if stage is not None:
            stages.append(stage)

    order = _topological_order(stages)
    overall = _overall(entry, stages)
    return ExperimentState(entry=entry, stages=stages, order=order, overall=overall)


def _stage_names(store: Store) -> list[str]:
    names: set[str] = set()
    for directory_name in ("stages", "attempts"):
        directory = store.root / directory_name
        if directory.exists():
            names.update(path.stem for path in directory.glob("*.json") if path.is_file())
    return sorted(names)


def _load_stage(entry: ExperimentEntry, store: Store, name: str) -> StageState | None:
    try:
        success = store.read_success(name)
        attempt = store.read_attempt(name)
    except CorruptStore:
        return StageState(
            name=name,
            status="corrupt",
            artifacts=[],
            committed_at=None,
            upstreams=[],
        )

    if success is None and attempt is None:
        return None

    artifacts: list[ArtifactState] = []
    committed_at: datetime | None = None
    upstreams: list[str] = []
    if success is not None:
        artifacts = _artifacts(entry, success)
        committed_at = _parse_datetime(success.committed_at)
        upstreams = sorted(success.key_components.upstreams)

    if attempt is not None:
        status: StageStatus = "interrupted"
    elif success is not None:
        status = "ok" if all(artifact.exists for artifact in artifacts) else "artifact-missing"
    else:
        return None

    return StageState(
        name=name,
        status=status,
        artifacts=artifacts,
        committed_at=committed_at,
        upstreams=upstreams,
    )


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


def _topological_order(stages: list[StageState]) -> list[str]:
    nodes = {stage.name for stage in stages}
    incoming = {name: set[str]() for name in nodes}
    outgoing = {name: set[str]() for name in nodes}
    for stage in stages:
        for upstream in stage.upstreams:
            if upstream not in nodes:
                continue
            incoming[stage.name].add(upstream)
            outgoing[upstream].add(stage.name)

    ready = sorted(name for name, upstreams in incoming.items() if not upstreams)
    order: list[str] = []
    while ready:
        name = ready.pop(0)
        order.append(name)
        for downstream in sorted(outgoing[name]):
            incoming[downstream].remove(name)
            if not incoming[downstream]:
                ready.append(downstream)
        ready.sort()
    return order if len(order) == len(nodes) else sorted(nodes)


def _overall(entry: ExperimentEntry, stages: list[StageState]) -> OverallStatus:
    if entry.experiment_name is None:
        return "corrupt"
    if not stages:
        return "empty"
    return max(stages, key=lambda stage: _STATUS_PRIORITY[stage.status]).status
