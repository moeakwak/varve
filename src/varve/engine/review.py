"""Pure source-review planning and shared record persistence."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from varve.engine.state import SourceReviewState
from varve.matrix import PipelineGraph
from varve.models import ReviewRecord, SourceFingerprint

ReviewAction = Literal["reuse", "invalidate"]


class ReviewStore(Protocol):
    def write_review(self, stage: str, record: ReviewRecord) -> None: ...

    def read_review(self, stage: str) -> ReviewRecord | None: ...


@dataclass(frozen=True)
class ReviewCandidate:
    """A source observation complete enough to make a review decision."""

    stage: str
    base_stage: str
    review_observation: SourceFingerprint
    source_review: SourceReviewState


@dataclass(frozen=True)
class ReviewWrite:
    """One validated, fingerprint-bound review record write."""

    base_stage: str
    review_observation: SourceFingerprint
    decision: ReviewAction
    decided_at: str | None = None


@dataclass(frozen=True)
class ReviewGroupResult:
    """Natural-language summary data for one base stage."""

    canonical_target: str
    recorded: tuple[str, ...]
    already_decided: tuple[str, ...]
    did_not_need_review: tuple[str, ...]


@dataclass(frozen=True)
class SourceReviewResult:
    """Structured result returned by an explicit pipeline review command."""

    decision: ReviewAction
    groups: tuple[ReviewGroupResult, ...]
    recorded: tuple[str, ...]
    already_decided: tuple[str, ...]
    did_not_need_review: tuple[str, ...]

    @property
    def has_source_changes(self) -> bool:
        return bool(self.recorded or self.already_decided)

    def __len__(self) -> int:
        """Return the number of decisions recorded by this command."""

        return len(self.recorded)


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
    *,
    existing: dict[str, ReviewRecord] | None = None,
    decided_at: str | None = None,
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
        current = None if existing is None else existing.get(base_stage)
        if (
            current is not None
            and current.review_fingerprint == candidate.review_observation.fingerprint
            and current.decision == decision
        ):
            continue
        if candidate.source_review.decision == decision and current is not None:
            continue
        keep_decided_at = None
        if (
            current is not None
            and current.review_fingerprint == candidate.review_observation.fingerprint
            and current.decision == decision
        ):
            keep_decided_at = current.decided_at
        writes.append(
            ReviewWrite(
                base_stage=base_stage,
                review_observation=candidate.review_observation,
                decision=decision,
                decided_at=keep_decided_at or decided_at,
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
                decided_at=write.decided_at or decided_at,
            ),
        )


def plan_source_review(
    graph: PipelineGraph,
    targets: Sequence[str],
    candidates: Sequence[ReviewCandidate],
    decision: ReviewAction,
    *,
    existing: dict[str, ReviewRecord] | None = None,
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
    writes = plan_review_writes(selected_candidates, decision, existing=existing)
    recorded_set = {write.base_stage for write in writes}

    recorded: list[str] = []
    already: list[str] = []
    did_not_need: list[str] = []
    groups: list[ReviewGroupResult] = []
    for base in selected_bases:
        group = by_base_candidates.get(base, [])
        if not group:
            did_not_need.append(base)
            groups.append(
                ReviewGroupResult(
                    canonical_target=base,
                    recorded=(),
                    already_decided=(),
                    did_not_need_review=(base,),
                )
            )
            continue
        changed = any(item.source_review.relationship == "changed" for item in group)
        if not changed:
            did_not_need.append(base)
            groups.append(
                ReviewGroupResult(
                    canonical_target=base,
                    recorded=(),
                    already_decided=(),
                    did_not_need_review=(base,),
                )
            )
            continue
        if base in recorded_set:
            recorded.append(base)
            groups.append(
                ReviewGroupResult(
                    canonical_target=base,
                    recorded=(base,),
                    already_decided=(),
                    did_not_need_review=(),
                )
            )
        else:
            already.append(base)
            groups.append(
                ReviewGroupResult(
                    canonical_target=base,
                    recorded=(),
                    already_decided=(base,),
                    did_not_need_review=(),
                )
            )

    return writes, SourceReviewResult(
        decision=decision,
        groups=tuple(groups),
        recorded=tuple(recorded),
        already_decided=tuple(already),
        did_not_need_review=tuple(did_not_need),
    )
