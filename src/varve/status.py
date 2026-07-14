"""Structured, read-only pipeline status."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from functools import cached_property
from pathlib import Path
from typing import Literal, NamedTuple

from varve.command import ResolvedCommandContext
from varve.engine.runner import _KeyingSession, probe_pipeline
from varve.engine.state import (
    EffectiveStatus,
    ReviewDecision,
    SourceRelationship,
    SourceReviewState,
    aggregate_effective_status,
    effective_reason,
    effective_status,
)
from varve.matrix import ResolvedStageSelector
from varve.models import KeyComponents

SourceChange = Literal["changed", "added", "removed"]


def _aggregate_stage_review(cells: list[StageStatus]) -> SourceReviewState:
    changed = [cell.source_review for cell in cells if cell.source_review.relationship == "changed"]
    if not changed:
        relationship: SourceRelationship = (
            "current"
            if any(cell.source_review.relationship == "current" for cell in cells)
            else "not-applicable"
        )
        return SourceReviewState(relationship)
    decisions: set[ReviewDecision] = {review.decision for review in changed}
    if "none" in decisions:
        return SourceReviewState("changed")
    if len(decisions) > 1:
        raise ValueError("Inconsistent Review Decisions within one Stage")
    return SourceReviewState("changed", next(iter(decisions)))


class CellCoordinate(NamedTuple):
    axis: str
    value_id: str


@dataclass(frozen=True, slots=True)
class StageStatus:
    name: str
    base_name: str
    cell: tuple[CellCoordinate, ...]
    needs: tuple[str, ...]
    logical_needs: tuple[str, ...]
    status: EffectiveStatus
    reason: str
    summary_reason: str
    execution_reason: str
    source_review: SourceReviewState
    duration: float | None
    committed_at: datetime | None
    decision_key: str | None
    stored_key: str | None
    key_inputs: KeyComponents | None
    source_changes: dict[str, SourceChange]
    unavailable_reason: str | None
    failure: str | None = None
    batch_progress: tuple[int, int] | None = None


@dataclass(frozen=True)
class StageStatusGroup:
    base_name: str
    cells: tuple[StageStatus, ...]
    review: SourceReviewState

    @property
    def is_matrix(self) -> bool:
        return bool(self.cells[0].cell)

    @cached_property
    def status(self) -> EffectiveStatus:
        return aggregate_effective_status(tuple(cell.status for cell in self.cells))

    @cached_property
    def status_counts(self) -> tuple[tuple[EffectiveStatus, int], ...]:
        counts = Counter(cell.status for cell in self.cells)
        order = ("needs-review", "hit", "needs-run", "resume", "failed", "error")
        return tuple((status, counts[status]) for status in order if counts[status])

    @cached_property
    def duration(self) -> float | None:
        recorded = [cell.duration for cell in self.cells if cell.duration is not None]
        return sum(recorded) if recorded else None

    @cached_property
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
    selector: ResolvedStageSelector | None = None

    @cached_property
    def groups(self) -> tuple[StageStatusGroup, ...]:
        grouped: dict[str, list[StageStatus]] = {}
        for stage in self.stages:
            grouped.setdefault(stage.base_name, []).append(stage)
        return tuple(
            StageStatusGroup(
                base_name=base_name,
                cells=tuple(cells),
                review=_aggregate_stage_review(cells),
            )
            for base_name, cells in grouped.items()
        )

    @cached_property
    def status(self) -> EffectiveStatus:
        return aggregate_effective_status(tuple(stage.status for stage in self.stages))

    @property
    def complete(self) -> bool:
        return bool(self.stages) and all(stage.status == "hit" for stage in self.stages)

    @cached_property
    def duration(self) -> float | None:
        if not self.stages or any(stage.duration is None for stage in self.stages):
            return None
        return sum(stage.duration for stage in self.stages if stage.duration is not None)

    @cached_property
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
    stages: list[StageStatus] = []
    for probe in probes:
        if selected_names is not None and probe.stage not in selected_names:
            continue
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
        batch_progress = probe.decision.progress if not spec.cell and spec.kind == "batch" else None
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
                execution_reason=execution_reason,
                source_review=probe.source_review,
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
                batch_progress=batch_progress,
            )
        )
    return PipelineStatus(
        pipeline=context.pipeline.__name__,
        branch=context.resolved.branch,
        output_root=context.output_root,
        stages=tuple(stages),
        selector=resolved_selector,
    )
