"""Pure cache-state decisions for varve stages."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal

from varve.keying.fingerprint import file_digest_view
from varve.models import AttemptMarker, BatchRecord, KeyComponents, SuccessRecord

ExecutionStatus = Literal[
    "hit",
    "needs-run",
    "resume",
    "failed",
    "error",
]
EffectiveStatus = Literal[
    "hit",
    "needs-review",
    "needs-run",
    "resume",
    "failed",
    "error",
]
SourceRelationship = Literal["not-applicable", "current", "changed"]
ReviewDecision = Literal["none", "accept", "reject"]


@dataclass(frozen=True)
class SourceReviewState:
    """The observed source relationship and any decision bound to it."""

    relationship: SourceRelationship
    decision: ReviewDecision = "none"

    def __post_init__(self) -> None:
        if self.relationship != "changed" and self.decision != "none":
            object.__setattr__(self, "decision", "none")


# Least to most severe. Every aggregate status view must use this order so a
# folded group and the dashboard cannot disagree about the state to surface.
EXECUTION_STATUS_SEVERITY: tuple[ExecutionStatus, ...] = (
    "hit",
    "needs-run",
    "resume",
    "failed",
    "error",
)
EFFECTIVE_STATUS_SEVERITY: tuple[EffectiveStatus, ...] = (
    "hit",
    "needs-run",
    "resume",
    "failed",
    "error",
)


def _aggregate_status(statuses, severity):
    return max(statuses, key=severity.index) if statuses else "hit"


def aggregate_execution_status(statuses: Sequence[ExecutionStatus]) -> ExecutionStatus:
    """Return the most severe execution status, treating no stages as a hit."""

    return _aggregate_status(statuses, EXECUTION_STATUS_SEVERITY)


def aggregate_effective_status(statuses: Sequence[EffectiveStatus]) -> EffectiveStatus:
    """Aggregate successfully probed effective states with review-gate priority."""

    if "needs-review" in statuses:
        return "needs-review"
    return _aggregate_status(statuses, EFFECTIVE_STATUS_SEVERITY)


def effective_status(
    execution_status: ExecutionStatus,
    source_review: SourceReviewState,
) -> EffectiveStatus:
    """Overlay source review semantics without changing the cache decision."""

    if source_review.relationship != "changed" or source_review.decision == "accept":
        return execution_status
    if source_review.decision == "none":
        return "needs-review"
    return "needs-run"


def effective_reason(execution_reason: str, source_review: SourceReviewState) -> str:
    """Return the summary reason for an effective stage status."""

    if source_review.relationship == "changed" and source_review.decision != "accept":
        return "source-changed"
    return execution_reason


@dataclass(frozen=True)
class Decision:
    status: ExecutionStatus
    reason: str
    resume_skip: frozenset[int] = field(default_factory=frozenset)
    resume_total: int | None = None

    @property
    def display_reason(self) -> str:
        if not self.resume_skip:
            return self.reason
        completed = len(self.resume_skip)
        progress = (
            f"{completed}/{self.resume_total}"
            if self.resume_total is not None
            else f"{completed} completed"
        )
        if self.status == "resume":
            return progress
        return f"{self.reason} · resume {progress}"


def decide(
    *,
    kind: Literal["single", "batch"],
    current_key: str,
    current_components: KeyComponents,
    success: SuccessRecord | None,
    attempt: AttemptMarker | None,
    produces_exist: bool = True,
    partial: dict[int, BatchRecord] | None = None,
    output_exists: Callable[[str], bool] | None = None,
    artifacts_match: bool = True,
    failure: object | None = None,
) -> Decision:
    outputs_exist = produces_exist
    if kind == "batch" and success is not None and success.input_key == current_key:
        assert success.outputs is not None
        assert output_exists is not None
        outputs_exist = all(output_exists(output.path) for output in success.outputs)
    skip = frozenset(partial or ())
    totals = {record.total for record in (partial or {}).values() if record.total is not None}
    total = next(iter(totals)) if len(totals) == 1 else None
    if failure is not None:
        return Decision("failed", "stage-failed", skip, total)
    if attempt is not None:
        return (
            Decision("resume", "resume", skip, total)
            if partial
            else Decision("needs-run", "interrupted")
        )
    if success is not None and success.input_key == current_key:
        if not outputs_exist:
            return Decision("needs-run", "artifact-missing")
        return (
            Decision("hit", "hit") if artifacts_match else Decision("needs-run", "artifact-changed")
        )
    if partial:
        assert output_exists is not None
        skip = frozenset(
            index
            for index, batch in partial.items()
            if all(output_exists(path) for path in batch.yielded)
        )
        return Decision("resume", "resume", skip, total)
    if success is None:
        return Decision("needs-run", "no-cache")
    return Decision("needs-run", invalidation_reason(success.key_components, current_components))


def invalidation_reason(old: KeyComponents, new: KeyComponents) -> str:
    for label, before, after in (
        ("config", old.config, new.config),
        ("value", old.values, new.values),
    ):
        for name in sorted(set(before) | set(after)):
            if before.get(name) != after.get(name):
                return f"{label}: {name} {before.get(name)!r} -> {after.get(name)!r}"
    old_files = file_digest_view(old.inputs)
    new_files = file_digest_view(new.inputs)
    if old_files != new_files:
        for name in sorted(set(old_files) | set(new_files)):
            if old_files.get(name) != new_files.get(name):
                return f"input: {name} changed"
        return "inputs-changed"
    for name in sorted(set(old.upstreams) | set(new.upstreams)):
        if old.upstreams.get(name) != new.upstreams.get(name):
            return f"upstream '{name}' changed"
    return "inputs-changed"
