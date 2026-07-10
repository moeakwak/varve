"""Pure cache-state decisions for varve stages."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from varve.keying.fingerprint import file_digest_view
from varve.models import AttemptMarker, BatchRecord, KeyComponents, SuccessRecord

Status = Literal[
    "dirty",
    "hit",
    "artifact-missing",
    "stale",
    "no-cache",
    "resume",
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
    success: SuccessRecord | None,
    partial: dict[int, BatchRecord] | None,
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
        return Decision("artifact-missing", "artifact missing", frozenset(existing))

    if partial is None:
        return Decision("no-cache", "no cache")

    skip = {
        index
        for index, batch in partial.items()
        if all(output_exists(path) for path in batch.yielded)
    }
    return Decision("resume", "resume", frozenset(skip))


def _with_source(reason: str, *, source_changed: bool) -> str:
    return f"{reason} (+ source)" if source_changed else reason


def invalidation_reason(old: KeyComponents, new: KeyComponents) -> str:
    source_changed = old.source != new.source
    for name in sorted(set(old.config) | set(new.config)):
        if old.config.get(name) != new.config.get(name):
            return _with_source(
                f"config: {name} {old.config.get(name)!r} -> {new.config.get(name)!r}",
                source_changed=source_changed,
            )
    old_files = file_digest_view(old.files)
    new_files = file_digest_view(new.files)
    if old_files != new_files:
        for name in sorted(set(old_files) | set(new_files)):
            if old_files.get(name) != new_files.get(name):
                return _with_source(
                    f"file: {name} changed",
                    source_changed=source_changed,
                )
        return _with_source("file changed", source_changed=source_changed)
    for name in sorted(set(old.values) | set(new.values)):
        if old.values.get(name) != new.values.get(name):
            return _with_source(
                f"value: {name} {old.values.get(name)!r} -> {new.values.get(name)!r}",
                source_changed=source_changed,
            )
    for name in sorted(set(old.upstreams) | set(new.upstreams)):
        if old.upstreams.get(name) != new.upstreams.get(name):
            return _with_source(
                f"upstream '{name}' changed",
                source_changed=source_changed,
            )
    if source_changed:
        return "source changed"
    return "content key changed"
