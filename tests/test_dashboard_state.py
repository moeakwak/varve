from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from varve import Pipeline, stage
from varve.dashboard.models import PipelineEntry
from varve.dashboard.state import load_state
from varve.engine.runner import StageProbe, run
from varve.engine.state import Decision, SourceReviewState
from varve.models import (
    ArtifactFingerprint,
    KeyComponents,
    ProducedPath,
    SourceFingerprint,
    SuccessRecord,
)
from varve.status import legacy_source_review
from varve.store.store import Store


class Config(BaseModel):
    pass


class Args(BaseModel):
    pass


class Demo(Pipeline):
    Config = Config
    Args = Args

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text("sample", encoding="utf-8")

    @stage(needs="sample", produces="summary.txt")
    def summary(self, ctx):
        (ctx.out / "summary.txt").write_text("summary", encoding="utf-8")


def _entry(output_root: Path, *, module: str | None = None) -> PipelineEntry:
    return PipelineEntry(
        output_root=output_root,
        pipeline_id="demo",
        pipeline_name="Demo",
        branch="main",
        module=module if module is not None else Demo.__module__,
    )


def _components() -> KeyComponents:
    return KeyComponents(config={}, inputs={}, values={}, upstreams={})


def _artifact(path: str) -> ArtifactFingerprint:
    return ArtifactFingerprint(root=path, kind="file", manifest=[], fingerprint=f"hash:{path}")


def _single(stage_name: str, *, elapsed: float | None = None) -> SuccessRecord:
    return SuccessRecord(
        pipeline="Demo",
        stage=stage_name,
        kind="single",
        input_key=f"sha256:{stage_name}",
        key_components=_components(),
        executed_source_fingerprint=SourceFingerprint(fingerprint="source", files=[]),
        artifact_fingerprint="artifacts",
        produces=[
            ProducedPath(
                path=f"{stage_name}.txt", kind="file", artifact=_artifact(f"{stage_name}.txt")
            )
        ],
        committed_at="2026-06-24T10:00:00+00:00",
        elapsed=elapsed,
    )


def _probe(stage: str, decision: Decision, previous: SuccessRecord | None) -> StageProbe:
    return StageProbe(
        stage=stage,
        decision=decision,
        decision_key=None,
        components=None,
        previous=previous,
        source_fingerprint=SourceFingerprint(fingerprint="source", files=[]),
        source_review=SourceReviewState("current"),
    )


@pytest.mark.parametrize(
    ("state", "legacy"),
    [
        (SourceReviewState("not-applicable"), "confirmed"),
        (SourceReviewState("current"), "confirmed"),
        (SourceReviewState("changed"), "pending"),
        (SourceReviewState("changed", "accept"), "accepted"),
        (SourceReviewState("changed", "reject"), "rerun-required"),
    ],
)
def test_legacy_source_review_is_converted_only_at_the_view_boundary(
    state: SourceReviewState,
    legacy: str,
) -> None:
    assert legacy_source_review(state) == legacy


def test_load_state_uses_engine_outcomes_and_topo_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "demo" / "out" / "main"
    store = Store(output_root)
    store.ensure_initialized("Demo", module=Demo.__module__)
    (output_root / "sample.txt").parent.mkdir(parents=True, exist_ok=True)
    (output_root / "sample.txt").write_text("sample", encoding="utf-8")
    store.write_success(_single("sample"))

    def fake_probe_pipeline(*args, **kwargs):
        del args, kwargs
        return [
            _probe("sample", Decision("hit", "hit"), store.read_success("sample")),
            _probe("summary", Decision("needs-run", "inputs-changed"), None),
        ]

    monkeypatch.setattr("varve.dashboard.state.probe_pipeline", fake_probe_pipeline)

    state = load_state(_entry(output_root))

    assert state.status == "needs-run"
    assert state.error is None
    assert [stage.name for stage in state.stages] == ["sample", "summary"]
    assert [stage.status for stage in state.stages] == ["hit", "needs-run"]
    assert [stage.reason for stage in state.stages] == ["hit", "inputs-changed"]
    assert state.stages[0].artifacts[0].path == Path("sample.txt")
    assert state.stages[0].artifacts[0].exists is True
    assert state.stages[0].committed_at is not None
    assert state.stages[1].upstreams == ["sample"]


def test_load_state_reads_stage_elapsed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "demo" / "out" / "main"
    store = Store(output_root)
    store.ensure_initialized("Demo", module=Demo.__module__)
    store.write_success(_single("sample", elapsed=1.25))

    def fake_probe_pipeline(*args, **kwargs):
        del args, kwargs
        return [
            _probe("sample", Decision("hit", "hit"), store.read_success("sample")),
            _probe("summary", Decision("needs-run", "no-cache"), None),
        ]

    monkeypatch.setattr("varve.dashboard.state.probe_pipeline", fake_probe_pipeline)

    state = load_state(_entry(output_root))

    assert state.stages[0].elapsed == 1.25
    assert state.stages[1].elapsed is None


def test_load_state_matches_engine_status_for_real_run(tmp_path: Path) -> None:
    output_base = tmp_path / "real" / "out"
    output_root = output_base / "main"

    run(Demo, Config(), args=Args(), cli_out=output_base)

    state = load_state(_entry(output_root))

    assert state.status == "hit"
    assert [(stage.name, stage.status, stage.reason) for stage in state.stages] == [
        ("sample", "hit", "hit"),
        ("summary", "hit", "hit"),
    ]


def test_load_state_reports_manifest_phase_for_manifest_errors(tmp_path: Path) -> None:
    state = load_state(
        PipelineEntry(
            output_root=tmp_path,
            pipeline_id="demo",
            pipeline_name="Demo",
            branch="main",
            module=Demo.__module__,
            manifest_error="bad json",
        )
    )

    assert state.status == "error"
    assert state.error is not None
    assert state.error.phase == "manifest"
    assert state.stages == []


def test_load_state_reports_manifest_phase_for_missing_module(tmp_path: Path) -> None:
    state = load_state(
        PipelineEntry(
            output_root=tmp_path,
            pipeline_id="demo",
            pipeline_name="Demo",
            branch="main",
            module=None,
        )
    )

    assert state.status == "error"
    assert state.error is not None
    assert state.error.phase == "manifest"
    assert state.stages == []


def test_load_state_reports_import_phase(tmp_path: Path) -> None:
    state = load_state(_entry(tmp_path, module="varve.no_such_module"))

    assert state.status == "error"
    assert state.error is not None
    assert state.error.phase == "import"


def test_load_state_reports_resolve_phase(tmp_path: Path) -> None:
    entry = _entry(tmp_path / "demo" / "out" / "missing")
    entry.branch = "missing"

    state = load_state(entry)

    assert state.status == "error"
    assert state.error is not None
    assert state.error.phase == "resolve"


def test_load_state_reports_evaluate_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "demo" / "out" / "main"

    def fail_probe_pipeline(*args, **kwargs):
        raise RuntimeError("engine failed")

    monkeypatch.setattr("varve.dashboard.state.probe_pipeline", fail_probe_pipeline)

    state = load_state(_entry(output_root))

    assert state.status == "error"
    assert state.error is not None
    assert state.error.phase == "evaluate"
    assert "engine failed" in state.error.message
