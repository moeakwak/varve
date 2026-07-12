"""Pure cache-state decisions for varve stages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from varve.keying.fingerprint import file_digest_view
from varve.models import AttemptMarker, BatchRecord, KeyComponents, SuccessRecord

Status = Literal[
    "hit",
    "needs-run",
    "resume",
    "failed",
    "error",
]

# Least to most severe. Every aggregate status view must use this order so a
# folded group and the dashboard cannot disagree about the state to surface.
STATUS_SEVERITY: tuple[Status, ...] = (
    "hit",
    "needs-run",
    "resume",
    "failed",
    "error",
)
_STATUS_SEVERITY = {status: index for index, status in enumerate(STATUS_SEVERITY)}


def aggregate_status(statuses: list[Status] | tuple[Status, ...]) -> Status:
    """Return the most severe status, treating an empty aggregate as a hit."""

    if not statuses:
        return "hit"
    return max(statuses, key=_STATUS_SEVERITY.__getitem__)


@dataclass(frozen=True)
class Decision:
    status: Status
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


def _partial_total(partial: dict[int, BatchRecord] | None) -> int | None:
    totals = {record.total for record in (partial or {}).values() if record.total is not None}
    return next(iter(totals)) if len(totals) == 1 else None


def decide_single(
    *,
    current_key: str,
    current_components: KeyComponents,
    success: SuccessRecord | None,
    attempt: AttemptMarker | None,
    produces_exist: bool,
    artifacts_match: bool = True,
    failure: object | None = None,
) -> Decision:
    if failure is not None:
        return Decision("failed", "stage-failed")
    if attempt is not None:
        return Decision("needs-run", "interrupted")
    if success is None:
        return Decision("needs-run", "no-cache")
    if success.input_key == current_key and produces_exist:
        return (
            Decision("hit", "hit") if artifacts_match else Decision("needs-run", "artifact-changed")
        )
    if success.input_key == current_key:
        return Decision("needs-run", "artifact-missing")
    return Decision("needs-run", invalidation_reason(success.key_components, current_components))


def decide_batch(
    *,
    current_key: str,
    current_components: KeyComponents,
    success: SuccessRecord | None,
    partial: dict[int, BatchRecord] | None,
    attempt: AttemptMarker | None,
    output_exists: Callable[[str], bool],
    artifacts_match: bool = True,
    failure: object | None = None,
) -> Decision:
    if failure is not None:
        skip = frozenset(partial) if partial is not None else frozenset()
        return Decision("failed", "stage-failed", skip, _partial_total(partial))
    if attempt is not None:
        if partial:
            return Decision(
                "resume",
                "resume",
                frozenset(partial),
                _partial_total(partial),
            )
        return Decision("needs-run", "interrupted")

    if success is not None:
        if success.input_key == current_key:
            assert success.outputs is not None
            output_paths_by_index: dict[int, list[str]] = {}
            for output in success.outputs:
                output_paths_by_index.setdefault(output.index, []).append(output.path)
            existing = {
                index
                for index, paths in output_paths_by_index.items()
                if all(output_exists(path) for path in paths)
            }
            if len(existing) == len(output_paths_by_index):
                return (
                    Decision("hit", "hit")
                    if artifacts_match
                    else Decision("needs-run", "artifact-changed")
                )
            return Decision("needs-run", "artifact-missing")

    if partial:
        skip = {
            index
            for index, batch in partial.items()
            if all(output_exists(path) for path in batch.yielded)
        }
        return Decision("resume", "resume", frozenset(skip), _partial_total(partial))
    if success is not None:
        return Decision(
            "needs-run", invalidation_reason(success.key_components, current_components)
        )
    return Decision("needs-run", "no-cache")


def invalidation_reason(old: KeyComponents, new: KeyComponents) -> str:
    for name in sorted(set(old.config) | set(new.config)):
        if old.config.get(name) != new.config.get(name):
            return f"config: {name} {old.config.get(name)!r} -> {new.config.get(name)!r}"
    old_files = file_digest_view(old.inputs)
    new_files = file_digest_view(new.inputs)
    if old_files != new_files:
        for name in sorted(set(old_files) | set(new_files)):
            if old_files.get(name) != new_files.get(name):
                return f"input: {name} changed"
        return "inputs-changed"
    for name in sorted(set(old.values) | set(new.values)):
        if old.values.get(name) != new.values.get(name):
            return f"value: {name} {old.values.get(name)!r} -> {new.values.get(name)!r}"
    for name in sorted(set(old.upstreams) | set(new.upstreams)):
        if old.upstreams.get(name) != new.upstreams.get(name):
            return f"upstream '{name}' changed"
    return "inputs-changed"
