from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from varve import Experiment, stage
from varve.dashboard.models import ExperimentEntry
from varve.dashboard.state import load_state
from varve.engine.runner import StageOutcome, run
from varve.models import KeyComponents, ProducedPath, SuccessRecord
from varve.store.store import Store


class Config(BaseModel):
    pass


class Args(BaseModel):
    pass


class Demo(Experiment):
    Config = Config
    Args = Args

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text("sample", encoding="utf-8")

    @stage(needs="sample", produces="summary.txt")
    def summary(self, ctx):
        (ctx.out / "summary.txt").write_text("summary", encoding="utf-8")


def _entry(output_root: Path, *, module: str | None = None) -> ExperimentEntry:
    return ExperimentEntry(
        output_root=output_root,
        experiment_id="demo",
        experiment_name="Demo",
        branch="main",
        module=module if module is not None else Demo.__module__,
    )


def _components() -> KeyComponents:
    return KeyComponents(source={}, config={}, files={}, values={}, upstreams={})


def _single(stage_name: str, *, elapsed: float | None = None) -> SuccessRecord:
    return SuccessRecord(
        experiment="Demo",
        stage=stage_name,
        kind="single",
        content_key=f"sha256:{stage_name}",
        key_components=_components(),
        produces=[ProducedPath(path=f"{stage_name}.txt", kind="file")],
        committed_at="2026-06-24T10:00:00+00:00",
        elapsed=elapsed,
    )


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

    def fake_evaluate_state(*args, **kwargs):
        return [
            StageOutcome("sample", "hit", "hit", None),
            StageOutcome("summary", "stale", "source changed", None),
        ]

    monkeypatch.setattr("varve.dashboard.state.evaluate_state", fake_evaluate_state)

    state = load_state(_entry(output_root))

    assert state.status == "stale"
    assert state.error is None
    assert [stage.name for stage in state.stages] == ["sample", "summary"]
    assert [stage.status for stage in state.stages] == ["hit", "stale"]
    assert [stage.reason for stage in state.stages] == ["hit", "source changed"]
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

    monkeypatch.setattr(
        "varve.dashboard.state.evaluate_state",
        lambda *args, **kwargs: [
            StageOutcome("sample", "hit", "hit", None),
            StageOutcome("summary", "no-cache", "no cache", None),
        ],
    )

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
        ExperimentEntry(
            output_root=tmp_path,
            experiment_id="demo",
            experiment_name="Demo",
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
        ExperimentEntry(
            output_root=tmp_path,
            experiment_id="demo",
            experiment_name="Demo",
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

    def fail_evaluate_state(*args, **kwargs):
        raise RuntimeError("engine failed")

    monkeypatch.setattr("varve.dashboard.state.evaluate_state", fail_evaluate_state)

    state = load_state(_entry(output_root))

    assert state.status == "error"
    assert state.error is not None
    assert state.error.phase == "evaluate"
    assert "engine failed" in state.error.message
