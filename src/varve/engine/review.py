"""Pure source-review planning and result grouping."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property
from typing import Literal, NamedTuple

from varve.engine.state import SourceReviewState
from varve.matrix import PipelineGraph
from varve.models import SourceFingerprint

ReviewAction = Literal["reuse", "invalidate"]
ReviewOutcome = Literal["recorded", "already-decided", "not-needed"]


class ReviewCandidate(NamedTuple):
    """A source observation complete enough to make a review decision."""

    base_stage: str
    review_observation: SourceFingerprint
    source_review: SourceReviewState


class ReviewStageResult(NamedTuple):
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

    @cached_property
    def recorded(self) -> tuple[str, ...]:
        return self._stages("recorded")

    @cached_property
    def already_decided(self) -> tuple[str, ...]:
        return self._stages("already-decided")

    @cached_property
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


def plan_source_review(
    graph: PipelineGraph,
    targets: Sequence[str],
    candidates: Sequence[ReviewCandidate],
    decision: ReviewAction,
) -> tuple[tuple[tuple[str, SourceFingerprint], ...], SourceReviewResult]:
    """Build a complete explicit-review plan and renderer-facing result."""

    if decision not in {"reuse", "invalidate"}:
        raise ValueError(f"Unknown source review decision: {decision}")
    validated = validate_base_stage_targets(graph, targets)
    changed_by_base: dict[str, list[ReviewCandidate]] = {}
    for candidate in candidates:
        if candidate.source_review.relationship == "changed":
            changed_by_base.setdefault(candidate.base_stage, []).append(candidate)
    selected_bases = validated or tuple(
        base
        for base in dict.fromkeys(
            graph.stages[stage].base_name or stage for stage in graph.topo_order()
        )
        if base in changed_by_base
    )

    writes: list[tuple[str, SourceFingerprint]] = []
    stages: list[ReviewStageResult] = []
    for base in selected_bases:
        changed = changed_by_base.get(base, [])
        fingerprints = {item.review_observation.fingerprint for item in changed}
        if len(fingerprints) > 1:
            raise ValueError(f"Inconsistent review fingerprints for Stage {base!r}")
        if not changed:
            outcome: ReviewOutcome = "not-needed"
        elif changed[0].source_review.decision == decision:
            outcome = "already-decided"
        else:
            writes.append((base, changed[0].review_observation))
            outcome = "recorded"
        stages.append(ReviewStageResult(stage=base, outcome=outcome))

    return tuple(writes), SourceReviewResult(decision, tuple(stages))
