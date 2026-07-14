"""Structured, read-only pipeline status."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from varve.command import ResolvedCommandContext
from varve.engine.runner import _KeyingSession, probe_pipeline
from varve.engine.state import (
    EffectiveStatus,
    ExecutionStatus,
    ReviewDecision,
    SourceRelationship,
    aggregate_effective_status,
    effective_reason,
    effective_status,
)
from varve.matrix import ResolvedStageSelector
from varve.models import KeyComponents

SourceChange = Literal["changed", "added", "removed"]


def _aggregate_stage_review(cells: list[StageStatus]) -> StageReviewStatus:
    review_changes: dict[str, SourceChange] = {}
    relationship: SourceRelationship = "not-applicable"
    decisions: set[ReviewDecision] = set()
    for cell in cells:
        for path, change in cell.source_changes.items():
            if path.startswith("review/"):
                review_changes[path.removeprefix("review/")] = change
        if cell.source_relationship != "changed":
            if relationship == "not-applicable" and cell.source_relationship == "current":
                relationship = "current"
            continue
        relationship = "changed"
        decisions.add(cell.source_decision)
    decision: ReviewDecision = "none"
    if "none" not in decisions and len(decisions) == 1:
        decision = next(iter(decisions))
    elif "none" not in decisions and decisions:
        raise ValueError("Inconsistent Review Decisions within one Stage")
    return StageReviewStatus(
        relationship=relationship,
        decision=decision,
        source_changes=review_changes,
    )


@dataclass(frozen=True)
class CellCoordinate:
    axis: str
    value_id: str


@dataclass(frozen=True)
class StageStatus:
    name: str
    base_name: str
    cell: tuple[CellCoordinate, ...]
    needs: tuple[str, ...]
    logical_needs: tuple[str, ...]
    status: EffectiveStatus
    reason: str
    summary_reason: str
    execution_status: ExecutionStatus
    execution_reason: str
    source_relationship: SourceRelationship
    source_decision: ReviewDecision
    duration: float | None
    committed_at: datetime | None
    decision_key: str | None
    stored_key: str | None
    key_inputs: KeyComponents | None
    source_changes: dict[str, SourceChange]
    unavailable_reason: str | None
    failure: str | None = None
    batch_completed: int | None = None
    batch_total: int | None = None


@dataclass(frozen=True)
class StageReviewStatus:
    relationship: SourceRelationship
    decision: ReviewDecision
    source_changes: dict[str, SourceChange]


@dataclass(frozen=True)
class StageStatusGroup:
    base_name: str
    axes: tuple[str, ...]
    logical_needs: tuple[str, ...]
    cells: tuple[StageStatus, ...]
    review: StageReviewStatus

    @property
    def is_matrix(self) -> bool:
        return bool(self.axes)

    @property
    def status(self) -> EffectiveStatus:
        return aggregate_effective_status(tuple(cell.status for cell in self.cells))

    @property
    def status_counts(self) -> tuple[tuple[EffectiveStatus, int], ...]:
        counts = Counter(cell.status for cell in self.cells)
        order = ("needs-review", "hit", "needs-run", "resume", "failed", "error")
        return tuple((status, counts[status]) for status in order if counts[status])

    @property
    def duration(self) -> float | None:
        recorded = [cell.duration for cell in self.cells if cell.duration is not None]
        return sum(recorded) if recorded else None

    @property
    def recorded_duration_count(self) -> int:
        return sum(cell.duration is not None for cell in self.cells)

    @property
    def reason(self) -> str:
        reasons = sorted({cell.summary_reason for cell in self.cells if cell.status == self.status})
        if not reasons:
            return "-"
        if len(reasons) == 1:
            return reasons[0]
        return f"{reasons[0]} · +{len(reasons) - 1} more"

    @property
    def hit_cells(self) -> int:
        return sum(cell.status == "hit" for cell in self.cells)

    @property
    def cell_count(self) -> int:
        return len(self.cells)

    @property
    def batch_completed(self) -> int | None:
        if self.is_matrix or len(self.cells) != 1:
            return None
        return self.cells[0].batch_completed

    @property
    def batch_total(self) -> int | None:
        if self.is_matrix or len(self.cells) != 1:
            return None
        return self.cells[0].batch_total


@dataclass(frozen=True)
class PipelineStatus:
    pipeline: str
    module: str
    branch: str
    output_root: Path
    stages: tuple[StageStatus, ...]
    selector: ResolvedStageSelector | None = None

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
                review=_aggregate_stage_review(cells),
            )
            for base_name, cells in grouped.items()
        )

    @property
    def status(self) -> EffectiveStatus:
        return aggregate_effective_status(tuple(stage.status for stage in self.stages))

    @property
    def complete(self) -> bool:
        return bool(self.stages) and all(stage.status == "hit" for stage in self.stages)

    @property
    def duration(self) -> float | None:
        if not self.stages or any(stage.duration is None for stage in self.stages):
            return None
        return sum(stage.duration for stage in self.stages if stage.duration is not None)

    @property
    def last_run(self) -> datetime | None:
        return max(
            (stage.committed_at for stage in self.stages if stage.committed_at is not None),
            default=None,
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


def _committed_at(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def collect_pipeline_status(
    context: ResolvedCommandContext,
    *,
    selector: str | ResolvedStageSelector | None = None,
    rehash: bool = False,
    session: _KeyingSession | None = None,
) -> PipelineStatus:
    """Probe one complete graph once, then apply an optional display selector."""

    resolved_selector = (
        context.graph.resolve_selector(selector) if isinstance(selector, str) else selector
    )
    selected_names = None if resolved_selector is None else set(resolved_selector.concrete_stages)
    probes = probe_pipeline(
        context.pipeline,
        context.resolved.config,
        args=context.args,
        out=context.output_root,
        graph=context.graph,
        force_rehash=rehash,
        _keying_session=session,
    )
    selected_probes = (
        probes
        if selected_names is None
        else tuple(probe for probe in probes if probe.stage in selected_names)
    )
    stages: list[StageStatus] = []
    for probe in selected_probes:
        spec = context.graph.stages[probe.stage]
        previous = probe.previous
        source_changes: dict[str, SourceChange] = {}
        if previous is not None:
            for prefix, old_fp, new_fp in (
                ("rerun", previous.executed_source.rerun, probe.source_observation.rerun),
                ("review", previous.executed_source.review, probe.source_observation.review),
            ):
                if old_fp.fingerprint == new_fp.fingerprint:
                    continue
                old_files = {item.path: item.digest for item in old_fp.files}
                new_files = {item.path: item.digest for item in new_fp.files}
                for path, change in source_component_changes(old_files, new_files).items():
                    source_changes[f"{prefix}/{path}"] = change
        execution_reason = probe.decision.display_reason
        status = effective_status(probe.decision.status, probe.source_review)
        reason = effective_reason(execution_reason, probe.source_review)
        batch_completed = None
        batch_total = None
        if (
            not spec.cell
            and spec.kind == "batch"
            and probe.decision.resume_skip
            and probe.decision.resume_total is not None
        ):
            batch_completed = len(probe.decision.resume_skip)
            batch_total = probe.decision.resume_total
        stages.append(
            StageStatus(
                name=probe.stage,
                base_name=spec.base_name or spec.name,
                cell=tuple(
                    CellCoordinate(axis=axis.name, value_id=axis.id_of(value))
                    for axis, value in spec.cell
                ),
                needs=spec.needs,
                logical_needs=spec.logical_needs,
                status=status,
                reason=reason,
                summary_reason=_summary_reason(reason, spec.need_cells),
                execution_status=probe.decision.status,
                execution_reason=execution_reason,
                source_relationship=probe.source_review.relationship,
                source_decision=probe.source_review.decision,
                duration=None if previous is None else previous.elapsed,
                committed_at=_committed_at(None if previous is None else previous.committed_at),
                decision_key=probe.decision_key,
                stored_key=previous.input_key if previous is not None else None,
                key_inputs=probe.components,
                source_changes=source_changes,
                unavailable_reason=probe.unavailable_reason,
                failure=(
                    None
                    if probe.failure is None
                    else f"{probe.failure.exception_type}: {probe.failure.message}"
                ),
                batch_completed=batch_completed,
                batch_total=batch_total,
            )
        )
    return PipelineStatus(
        pipeline=context.pipeline.__name__,
        module=context.pipeline.import_module_name(),
        branch=context.resolved.branch,
        output_root=context.output_root,
        stages=tuple(stages),
        selector=resolved_selector,
    )
