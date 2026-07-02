"""Pure cache-state decisions for varve stages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from varve.models import AttemptMarker, BatchRecord, KeyComponents, PartialMeta, SuccessRecord

Status = Literal[
    "dirty",
    "hit",
    "artifact-missing",
    "stale",
    "no-cache",
    "resume",
    "unrecoverable",
]


@dataclass(frozen=True)
class Decision:
    status: Status
    reason: str
    resume_skip: frozenset[int] = field(default_factory=frozenset)


def decide_single(
    *,
    current_key: str,
    current_components: KeyComponents,
    success: SuccessRecord | None,
    attempt: AttemptMarker | None,
    produces_exist: bool,
) -> Decision:
    if attempt is not None:
        return Decision("dirty", "dirty")
    if success is None:
        return Decision("no-cache", "no cache")
    if success.content_key == current_key and produces_exist:
        return Decision("hit", "hit")
    if success.content_key == current_key:
        return Decision("artifact-missing", "artifact missing")
    return Decision("stale", invalidation_reason(success.key_components, current_components))


def decide_batch(
    *,
    current_key: str,
    current_components: KeyComponents,
    current_partition: dict,
    run_key: str,
    partial_run_key: str | None,
    success: SuccessRecord | None,
    partial: tuple[PartialMeta, dict[int, BatchRecord]] | None,
    attempt: AttemptMarker | None,
    output_exists: Callable[[str], bool],
) -> Decision:
    if attempt is not None:
        return Decision("dirty", "dirty")

    if success is not None:
        if success.content_key != current_key:
            return Decision(
                "stale", invalidation_reason(success.key_components, current_components)
            )
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
            return Decision("hit", "hit")
        if current_partition != success.partition_values:
            return Decision("unrecoverable", "partition changed after artifact loss")
        return Decision("artifact-missing", "artifact missing", frozenset(existing))

    if partial is None:
        return Decision("no-cache", "no cache")

    if partial_run_key != run_key:
        return Decision("no-cache", "no cache")
    _meta, batches = partial
    skip = {
        index
        for index, batch in batches.items()
        if all(output_exists(path) for path in batch.yielded)
    }
    return Decision("resume", "resume", frozenset(skip))


def invalidation_reason(old: KeyComponents, new: KeyComponents) -> str:
    if old.source != new.source:
        return "source changed"
    for name in sorted(set(old.config) | set(new.config)):
        if old.config.get(name) != new.config.get(name):
            return f"config: {name} {old.config.get(name)!r} -> {new.config.get(name)!r}"
    if old.files != new.files:
        for name in sorted(set(old.files) | set(new.files)):
            if old.files.get(name) != new.files.get(name):
                return f"file: {name} changed"
        return "file changed"
    for name in sorted(set(old.values) | set(new.values)):
        if old.values.get(name) != new.values.get(name):
            return f"value: {name} {old.values.get(name)!r} -> {new.values.get(name)!r}"
    for name in sorted(set(old.upstreams) | set(new.upstreams)):
        if old.upstreams.get(name) != new.upstreams.get(name):
            return f"upstream '{name}' changed"
    return "content key changed"
