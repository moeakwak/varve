"""Pure source-review planning and shared record persistence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from varve.engine.state import SourceReviewState
from varve.matrix import PipelineGraph
from varve.models import ReviewRecord, SourceFingerprint

ReviewAction = Literal["reuse", "invalidate"]
ReviewOutcome = Literal["recorded", "already-decided", "not-needed"]


class ReviewStore(Protocol):
    def write_review(self, stage: str, record: ReviewRecord) -> None: ...


@dataclass(frozen=True)
class ReviewCandidate:
    """A source observation complete enough to make a review decision."""

    base_stage: str
    review_observation: SourceFingerprint
    source_review: SourceReviewState


@dataclass(frozen=True)
class ReviewWrite:
    """One validated, fingerprint-bound review record write."""

    base_stage: str
    review_observation: SourceFingerprint
    decision: ReviewAction


@dataclass(frozen=True)
class ReviewStageResult:
    """Natural-language summary data for one base stage."""

    stage: str
    outcome: ReviewOutcome


@dataclass(frozen=True)
class SourceReviewResult:
    """Structured result returned by an explicit pipeline review command."""

    decision: ReviewAction
    stages: tuple[ReviewStageResult, ...]

    def _stages(self, outcome: ReviewOutcome) -> tuple[str, ...]:
        return tuple(stage.stage for stage in self.stages if stage.outcome == outcome)

    @property
    def recorded(self) -> tuple[str, ...]:
        return self._stages("recorded")

    @property
    def already_decided(self) -> tuple[str, ...]:
        return self._stages("already-decided")

    @property
    def did_not_need_review(self) -> tuple[str, ...]:
        return self._stages("not-needed")

    @property
    def has_source_changes(self) -> bool:
        return bool(self.recorded or self.already_decided)


def validate_base_stage_targets(
    graph: PipelineGraph,
    targets: Sequence[str],
) -> tuple[str, ...]:
    """Validate and stably dedupe base-only review targets."""

    if not targets:
        return ()
    errors: list[str] = []
    for target in targets:
        if "@" in target:
            base = target.split("@", 1)[0]
            errors.append(
                f"Review Decision belongs to the whole Stage; use {base!r} instead of {target!r}"
            )
            continue
        if target not in graph.base_cells:
            errors.append(f"Unknown varve stage: {target}")
    if errors:
        raise ValueError("; ".join(errors))
    requested = set(targets)
    return tuple(
        base
        for base in dict.fromkeys(
            graph.stages[stage].base_name or stage for stage in graph.topo_order()
        )
        if base in requested
    )


def plan_review_writes(
    candidates: Sequence[ReviewCandidate],
    decision: ReviewAction,
) -> tuple[ReviewWrite, ...]:
    """Plan one write per base Stage for changed candidates only."""

    if decision not in {"reuse", "invalidate"}:
        raise ValueError(f"Unknown source review decision: {decision}")
    by_base: dict[str, ReviewCandidate] = {}
    for candidate in candidates:
        if candidate.source_review.relationship != "changed":
            continue
        previous = by_base.get(candidate.base_stage)
        if previous is None:
            by_base[candidate.base_stage] = candidate
            continue
        if previous.review_observation.fingerprint != candidate.review_observation.fingerprint:
            raise ValueError(f"Inconsistent review fingerprints for Stage {candidate.base_stage!r}")
    writes: list[ReviewWrite] = []
    for base_stage, candidate in by_base.items():
        if candidate.source_review.decision == decision:
            continue
        writes.append(
            ReviewWrite(
                base_stage=base_stage,
                review_observation=candidate.review_observation,
                decision=decision,
            )
        )
    return tuple(writes)


def apply_review_writes(
    store: ReviewStore,
    writes: Sequence[ReviewWrite],
    decided_at: str,
) -> None:
    """Atomically replace each planned record, preserving per-record integrity."""

    for write in writes:
        store.write_review(
            write.base_stage,
            ReviewRecord(
                review_fingerprint=write.review_observation.fingerprint,
                review_observation=write.review_observation,
                decision=write.decision,
                decided_at=decided_at,
            ),
        )


def plan_source_review(
    graph: PipelineGraph,
    targets: Sequence[str],
    candidates: Sequence[ReviewCandidate],
    decision: ReviewAction,
) -> tuple[tuple[ReviewWrite, ...], SourceReviewResult]:
    """Build a complete explicit-review plan and renderer-facing result."""

    validated = validate_base_stage_targets(graph, targets)
    by_base_candidates: dict[str, list[ReviewCandidate]] = {}
    for candidate in candidates:
        by_base_candidates.setdefault(candidate.base_stage, []).append(candidate)

    if validated:
        selected_bases = validated
    else:
        selected_bases = tuple(
            base
            for base, group in by_base_candidates.items()
            if any(item.source_review.relationship == "changed" for item in group)
        )
        # Keep topology order of first appearance in concrete topo order.
        topo_bases = tuple(
            dict.fromkeys(graph.stages[stage].base_name or stage for stage in graph.topo_order())
        )
        selected_bases = tuple(base for base in topo_bases if base in set(selected_bases))

    selected_candidates = [
        candidate for base in selected_bases for candidate in by_base_candidates.get(base, ())
    ]
    writes = plan_review_writes(selected_candidates, decision)
    recorded_set = {write.base_stage for write in writes}

    stages: list[ReviewStageResult] = []
    for base in selected_bases:
        group = by_base_candidates.get(base, [])
        changed = any(item.source_review.relationship == "changed" for item in group)
        outcome: ReviewOutcome = (
            "not-needed"
            if not changed
            else "recorded"
            if base in recorded_set
            else "already-decided"
        )
        stages.append(ReviewStageResult(stage=base, outcome=outcome))

    return writes, SourceReviewResult(
        decision=decision,
        stages=tuple(stages),
    )
