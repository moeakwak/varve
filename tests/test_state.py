from __future__ import annotations

from typing import Any

import pytest

from varve.engine.state import decide_batch, decide_single, invalidation_reason
from varve.models import (
    AttemptMarker,
    BatchRecord,
    FileFingerprint,
    KeyComponents,
    OutputHandle,
    ProducedPath,
    SuccessRecord,
)


def _components(
    *,
    source: dict[str, str] | None = None,
    config: dict[str, Any] | None = None,
    files: dict[str, list[FileFingerprint]] | None = None,
    values: dict[str, Any] | None = None,
    upstreams: dict[str, dict[str, str]] | None = None,
) -> KeyComponents:
    return KeyComponents(
        source=source or {},
        config=config or {},
        files=files or {},
        values=values or {},
        upstreams=upstreams or {},
    )


def _file(*, sha256: str, size: int = 1, mtime: float = 1.0) -> FileFingerprint:
    return FileFingerprint(
        path="/tmp/input.jsonl",
        size=size,
        mtime=mtime,
        sha256=sha256,
    )


def _single(key: str = "sha256:a") -> SuccessRecord:
    return SuccessRecord(
        pipeline="Demo",
        stage="sample",
        kind="single",
        content_key=key,
        key_components=_components(),
        produces=[ProducedPath(path="sample.txt", kind="file")],
        committed_at="now",
    )


def _batch(key: str = "sha256:a") -> SuccessRecord:
    return SuccessRecord(
        pipeline="Demo",
        stage="batch",
        kind="batch",
        content_key=key,
        key_components=_components(),
        outputs=[
            OutputHandle(index=0, path="part-0.txt"),
            OutputHandle(index=1, path="part-1.txt"),
        ],
        committed_at="now",
    )


def test_invalidation_reason_priority() -> None:
    assert (
        invalidation_reason(_components(source={"a": "1"}), _components(source={"a": "2"}))
        == "source changed"
    )
    assert (
        invalidation_reason(_components(config={"x": 1}), _components(config={"x": 2}))
        == "config: x 1 -> 2"
    )
    assert (
        invalidation_reason(_components(values={"v": 1}), _components(values={"v": 2}))
        == "value: v 1 -> 2"
    )
    assert (
        invalidation_reason(
            _components(upstreams={"sample": {"content_key": "1"}}),
            _components(upstreams={"sample": {"content_key": "2"}}),
        )
        == "upstream 'sample' changed"
    )


@pytest.mark.parametrize(
    ("update", "expected"),
    [
        (
            {"config": {"profile": "new"}},
            "config: profile 'old' -> 'new' (+ source)",
        ),
        (
            {"files": {"input": [_file(sha256="sha256:new")]}},
            "file: input changed (+ source)",
        ),
        ({"values": {"limit": 2}}, "value: limit 1 -> 2 (+ source)"),
        (
            {"upstreams": {"prepare": {"content_key": "new"}}},
            "upstream 'prepare' changed (+ source)",
        ),
    ],
)
def test_invalidation_reason_prefers_specific_change_and_notes_source(
    update: dict,
    expected: str,
) -> None:
    old = _components(
        source={"stage": "old"},
        config={"profile": "old"},
        files={"input": [_file(sha256="sha256:old")]},
        values={"limit": 1},
        upstreams={"prepare": {"content_key": "old"}},
    )

    new = old.model_copy(update={"source": {"stage": "new"}, **update})
    assert invalidation_reason(old, new) == expected


def test_invalidation_reason_ignores_file_metadata_drift() -> None:
    old_files = {"input": [_file(sha256="sha256:same")]}
    touched_files = {"input": [_file(sha256="sha256:same", size=2, mtime=2.0)]}

    assert (
        invalidation_reason(
            _components(source={"stage": "old"}, files=old_files),
            _components(source={"stage": "new"}, files=touched_files),
        )
        == "source changed"
    )
    assert (
        invalidation_reason(
            _components(files=old_files, values={"limit": 1}),
            _components(files=touched_files, values={"limit": 2}),
        )
        == "value: limit 1 -> 2"
    )


def test_decide_single_rows() -> None:
    marker = AttemptMarker(content_key="sha256:a", started_at="now", touched_existing=False)
    assert (
        decide_single(
            current_key="sha256:a",
            current_components=_components(),
            success=_single(),
            attempt=marker,
            produces_exist=True,
        ).status
        == "dirty"
    )
    assert (
        decide_single(
            current_key="sha256:a",
            current_components=_components(),
            success=_single(),
            attempt=None,
            produces_exist=True,
        ).status
        == "hit"
    )
    assert (
        decide_single(
            current_key="sha256:a",
            current_components=_components(),
            success=_single(),
            attempt=None,
            produces_exist=False,
        ).status
        == "artifact-missing"
    )
    assert (
        decide_single(
            current_key="sha256:b",
            current_components=_components(source={"x": "y"}),
            success=_single(),
            attempt=None,
            produces_exist=True,
        ).status
        == "stale"
    )
    assert (
        decide_single(
            current_key="sha256:a",
            current_components=_components(),
            success=None,
            attempt=None,
            produces_exist=False,
        ).status
        == "no-cache"
    )


def test_decide_batch_rows() -> None:
    success = _batch()
    exists = {"part-0.txt", "part-1.txt"}.__contains__
    assert (
        decide_batch(
            current_key="sha256:a",
            current_components=_components(),
            success=success,
            partial=None,
            attempt=None,
            output_exists=exists,
        ).status
        == "hit"
    )

    missing = {"part-0.txt"}.__contains__
    decision = decide_batch(
        current_key="sha256:a",
        current_components=_components(),
        success=success,
        partial=None,
        attempt=None,
        output_exists=missing,
    )
    assert decision.status == "artifact-missing"
    assert decision.resume_skip == frozenset({0})

    partial = {0: BatchRecord(index=0, yielded=["part-0.txt"], committed_at="now")}
    resume = decide_batch(
        current_key="sha256:a",
        current_components=_components(),
        success=None,
        partial=partial,
        attempt=None,
        output_exists=missing,
    )
    assert resume.status == "resume"
    assert resume.resume_skip == frozenset({0})

    marker = AttemptMarker(content_key="sha256:a", started_at="now", touched_existing=True)
    assert (
        decide_batch(
            current_key="sha256:a",
            current_components=_components(),
            success=success,
            partial=None,
            attempt=marker,
            output_exists=exists,
        ).status
        == "dirty"
    )

    assert (
        decide_batch(
            current_key="sha256:b",
            current_components=_components(source={"x": "y"}),
            success=success,
            partial=None,
            attempt=None,
            output_exists=exists,
        ).status
        == "stale"
    )
