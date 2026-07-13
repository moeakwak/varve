"""Pure source-review planning and shared record persistence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from varve.engine.state import SourceReviewState
from varve.matrix import PipelineGraph, ResolvedStageSelector
from varve.models import ReviewRecord, SourceFingerprint

ReviewAction = Literal["accept", "reject"]


class ReviewStore(Protocol):
    def write_review(self, stage: str, record: ReviewRecord) -> None: ...


@dataclass(frozen=True)
class ReviewCandidate:
    """A source observation complete enough to make a review decision."""

    stage: str
    base_stage: str
    source_fingerprint: SourceFingerprint
    source_review: SourceReviewState


@dataclass(frozen=True)
class ReviewWrite:
    """One validated, fingerprint-bound review record write."""

    stage: str
    source_fingerprint: SourceFingerprint
    decision: ReviewAction


@dataclass(frozen=True)
class ReviewGroupResult:
    """Natural-language summary data for one base stage or broad selector."""

    canonical_target: str
    base_stage: str
    matched_cells: tuple[str, ...]
    source_changed_cells: tuple[str, ...]
    recorded: tuple[str, ...]
    already_decided: tuple[str, ...]
    did_not_need_review: tuple[str, ...]
    failed_cells: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceReviewResult:
    """Structured result returned by an explicit pipeline review command."""

    decision: ReviewAction
    groups: tuple[ReviewGroupResult, ...]
    matched_cells: tuple[str, ...]
    source_changed_cells: tuple[str, ...]
    recorded: tuple[str, ...]
    already_decided: tuple[str, ...]
    did_not_need_review: tuple[str, ...]
    exact_target: str | None = None
    failed_cells: tuple[str, ...] = ()

    @property
    def has_source_changes(self) -> bool:
        return bool(self.source_changed_cells)

    @property
    def changed_decisions(self) -> bool:
        return bool(self.recorded)

    def __len__(self) -> int:
        """Return the number of decisions recorded by this command."""

        return len(self.recorded)


def plan_review_writes(
    candidates: Sequence[ReviewCandidate],
    decision: ReviewAction,
) -> tuple[ReviewWrite, ...]:
    """Plan idempotent writes for changed candidates only."""

    if decision not in {"accept", "reject"}:
        raise ValueError(f"Unknown source review decision: {decision}")
    return tuple(
        ReviewWrite(candidate.stage, candidate.source_fingerprint, decision)
        for candidate in candidates
        if candidate.source_review.relationship == "changed"
        and candidate.source_review.decision != decision
    )


def apply_review_writes(
    store: ReviewStore,
    writes: Sequence[ReviewWrite],
    decided_at: str,
) -> tuple[str, ...]:
    """Atomically replace each planned record, preserving per-record integrity."""

    written: list[str] = []
    for write in writes:
        store.write_review(
            write.stage,
            ReviewRecord(
                source_fingerprint=write.source_fingerprint.fingerprint,
                source_observation=write.source_fingerprint,
                decision=write.decision,
                decided_at=decided_at,
            ),
        )
        written.append(write.stage)
    return tuple(written)


def plan_source_review(
    graph: PipelineGraph,
    resolved_selectors: Sequence[ResolvedStageSelector],
    candidates: Sequence[ReviewCandidate],
    decision: ReviewAction,
) -> tuple[tuple[ReviewWrite, ...], SourceReviewResult]:
    """Build a complete explicit-review plan and renderer-facing result."""

    by_stage = {candidate.stage: candidate for candidate in candidates}
    topo_order = graph.topo_order()
    if resolved_selectors:
        selected_set = {
            stage for selector in resolved_selectors for stage in selector.concrete_stages
        }
    else:
        selected_set = {
            candidate.stage
            for candidate in candidates
            if candidate.source_review.relationship == "changed"
        }
    selected = tuple(stage for stage in topo_order if stage in selected_set)
    selected_candidates = tuple(by_stage[stage] for stage in selected)
    writes = plan_review_writes(selected_candidates, decision)
    recorded_set = {write.stage for write in writes}

    source_changed = tuple(
        candidate.stage
        for candidate in selected_candidates
        if candidate.source_review.relationship == "changed"
    )
    already = tuple(
        candidate.stage
        for candidate in selected_candidates
        if candidate.source_review.relationship == "changed"
        and candidate.source_review.decision == decision
    )
    did_not_need = tuple(
        candidate.stage
        for candidate in selected_candidates
        if candidate.source_review.relationship != "changed"
    )
    recorded = tuple(stage for stage in selected if stage in recorded_set)

    selectors_by_base: dict[str, list[ResolvedStageSelector]] = {}
    for selector in resolved_selectors:
        selectors_by_base.setdefault(selector.base_stage, []).append(selector)
    groups: list[ReviewGroupResult] = []
    ordered_bases = tuple(
        dict.fromkeys(graph.stages[stage].base_name or stage for stage in selected)
    )
    for base_stage in ordered_bases:
        base_selected = tuple(stage for stage in selected if stage in graph.base_cells[base_stage])
        if not base_selected:
            continue
        base_selectors = selectors_by_base.get(base_stage, [])
        canonical_target = base_stage
        if len(base_selectors) == 1 and set(base_selectors[0].concrete_stages) == set(
            base_selected
        ):
            canonical_target = base_selectors[0].canonical
        base_set = set(base_selected)
        groups.append(
            ReviewGroupResult(
                canonical_target=canonical_target,
                base_stage=base_stage,
                matched_cells=base_selected,
                source_changed_cells=tuple(stage for stage in source_changed if stage in base_set),
                recorded=tuple(stage for stage in recorded if stage in base_set),
                already_decided=tuple(stage for stage in already if stage in base_set),
                did_not_need_review=tuple(stage for stage in did_not_need if stage in base_set),
            )
        )

    exact_target = None
    if len(resolved_selectors) == 1 and resolved_selectors[0].is_concrete:
        exact_target = resolved_selectors[0].canonical
    return writes, SourceReviewResult(
        decision=decision,
        groups=tuple(groups),
        matched_cells=selected,
        source_changed_cells=source_changed,
        recorded=recorded,
        already_decided=already,
        did_not_need_review=did_not_need,
        exact_target=exact_target,
    )
