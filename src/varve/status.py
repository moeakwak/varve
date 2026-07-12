"""Structured, read-only pipeline status."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from varve.engine.runner import probe_pipeline
from varve.engine.state import STATUS_SEVERITY, Status, aggregate_status
from varve.matrix import PipelineGraph, build_graph
from varve.models import FileFingerprint
from varve.pipeline import Pipeline

SourceChange = Literal["changed", "added", "removed"]


@dataclass(frozen=True)
class KeyInputs:
    config: dict[str, Any]
    inputs: dict[str, list[FileFingerprint]]
    values: dict[str, Any]
    upstreams: dict[str, dict[str, str]]


@dataclass(frozen=True)
class CellCoordinate:
    axis: str
    value_id: str


@dataclass(frozen=True)
class StageStatus:
    name: str
    base_name: str
    cell: tuple[CellCoordinate, ...]
    kind: str
    needs: tuple[str, ...]
    logical_needs: tuple[str, ...]
    status: Status
    reason: str
    summary_reason: str
    duration: float | None
    decision_key: str | None
    stored_key: str | None
    key_inputs: KeyInputs | None
    source_changes: dict[str, SourceChange]
    unavailable_reason: str | None
    failure: str | None = None
    source_review: str = "confirmed"


@dataclass(frozen=True)
class StageStatusGroup:
    base_name: str
    axes: tuple[str, ...]
    logical_needs: tuple[str, ...]
    cells: tuple[StageStatus, ...]

    @property
    def is_matrix(self) -> bool:
        return bool(self.axes)

    @property
    def status(self) -> Status:
        statuses: tuple[Status, ...] = tuple(cell.status for cell in self.cells)
        return aggregate_status(statuses)

    @property
    def status_counts(self) -> tuple[tuple[Status, int], ...]:
        counts = {status: 0 for status in STATUS_SEVERITY}
        for cell in self.cells:
            counts[cell.status] += 1
        return tuple((status, counts[status]) for status in STATUS_SEVERITY if counts[status])

    @property
    def duration(self) -> float | None:
        recorded = [cell.duration for cell in self.cells if cell.duration is not None]
        return sum(recorded) if recorded else None

    @property
    def recorded_duration_count(self) -> int:
        return sum(cell.duration is not None for cell in self.cells)

    @property
    def review(self) -> str:
        pending = sum(cell.source_review == "pending" for cell in self.cells)
        if pending:
            return f"{pending} source-changed"
        states = {cell.source_review for cell in self.cells}
        return next(iter(states)) if len(states) == 1 else "mixed"

    @property
    def reason(self) -> str:
        reasons = sorted({cell.summary_reason for cell in self.cells if cell.status == self.status})
        if not reasons:
            return "-"
        if len(reasons) == 1:
            return reasons[0]
        return f"{reasons[0]} · +{len(reasons) - 1} more"


@dataclass(frozen=True)
class PipelineStatus:
    pipeline: str
    branch: str
    output_root: Path
    stages: tuple[StageStatus, ...]

    @property
    def groups(self) -> tuple[StageStatusGroup, ...]:
        grouped: dict[str, list[StageStatus]] = {}
        for stage in self.stages:
            grouped.setdefault(stage.base_name, []).append(stage)
        return tuple(
            StageStatusGroup(
                base_name=base_name,
                axes=tuple(coordinate.axis for coordinate in cells[0].cell),
                logical_needs=cells[0].logical_needs,
                cells=tuple(cells),
            )
            for base_name, cells in grouped.items()
        )


def source_component_changes(
    old: Mapping[str, str],
    new: Mapping[str, str],
) -> dict[str, SourceChange]:
    changes: dict[str, SourceChange] = {}
    for name in sorted(set(old) | set(new)):
        if name not in old:
            changes[name] = "added"
        elif name not in new:
            changes[name] = "removed"
        elif old[name] != new[name]:
            changes[name] = "changed"
    return changes


def _summary_reason(reason: str, need_cells: dict[str, tuple[str, ...]] | None) -> str:
    if reason.startswith("value: __varve_matrix_layout__"):
        return "matrix layout changed"
    for logical_need, concrete_needs in (need_cells or {}).items():
        for concrete_need in concrete_needs:
            prefix = f"upstream '{concrete_need}'"
            if reason.startswith(prefix):
                return f"upstream {logical_need}{reason[len(prefix) :]}"
    return reason


def collect_pipeline_status(
    pipeline: type[Pipeline],
    config: Any,
    *,
    args: Any,
    out: Path,
    branch: str,
    stage: str | None = None,
    graph: PipelineGraph | None = None,
    rehash: bool = False,
) -> PipelineStatus:
    """Collect decision keys and dependency descriptions without executing stages."""

    graph = graph or build_graph(pipeline)
    selected_names = None if stage is None else set(graph.names_for(stage))
    probes = probe_pipeline(
        pipeline,
        config,
        args=args,
        out=out,
        graph=graph,
        force_rehash=rehash,
    )
    selected_probes = (
        probes
        if selected_names is None
        else tuple(probe for probe in probes if probe.stage in selected_names)
    )
    stages: list[StageStatus] = []
    for probe in selected_probes:
        spec = graph.stages[probe.stage]
        components = probe.components
        key_inputs = (
            None
            if components is None
            else KeyInputs(
                config=components.config,
                inputs=components.inputs,
                values=components.values,
                upstreams=components.upstreams,
            )
        )
        previous = probe.previous
        source_changes: dict[str, SourceChange] = {}
        if probe.source_review == "pending" and previous is not None:
            old_files = {
                item.path: item.digest for item in previous.executed_source_fingerprint.files
            }
            new_files = {item.path: item.digest for item in probe.source_fingerprint.files}
            source_changes = source_component_changes(old_files, new_files)
        stages.append(
            StageStatus(
                name=probe.stage,
                base_name=spec.base_name or spec.name,
                cell=tuple(
                    CellCoordinate(axis=axis.name, value_id=axis.id_of(value))
                    for axis, value in spec.cell
                ),
                kind=spec.kind,
                needs=spec.needs,
                logical_needs=spec.logical_needs,
                status=probe.decision.status,
                reason=probe.decision.display_reason,
                summary_reason=_summary_reason(probe.decision.display_reason, spec.need_cells),
                duration=(None if previous is None else previous.elapsed),
                decision_key=probe.decision_key,
                stored_key=previous.input_key if previous is not None else None,
                key_inputs=key_inputs,
                source_changes=source_changes,
                unavailable_reason=probe.unavailable_reason,
                failure=(
                    None
                    if probe.failure is None
                    else f"{probe.failure.exception_type}: {probe.failure.message}"
                ),
                source_review=probe.source_review,
            )
        )
    return PipelineStatus(
        pipeline=pipeline.__name__,
        branch=branch,
        output_root=out,
        stages=tuple(stages),
    )
