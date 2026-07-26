from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from varve import Pipeline, stage
from varve.dashboard.models import PipelineEntry
from varve.dashboard.state import (
    load_state,
    resolve_module_entry,
    resolve_structure_pipeline,
)
from varve.engine.runner import _KeyingSession, run
from varve.status import PipelineStatus


class Config(BaseModel):
    pass


class Demo(Pipeline):
    Config = Config

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text("sample", encoding="utf-8")

    @stage(needs="sample", produces="summary.txt")
    def summary(self, ctx):
        (ctx.out / "summary.txt").write_text("summary", encoding="utf-8")


def _entry(
    output_root: Path,
    *,
    module: str | None = None,
    name: str | None = None,
    pipeline_name: str | None = "Demo",
    branch: str = "main",
) -> PipelineEntry:
    return PipelineEntry(
        output_root=output_root,
        pipeline_id="demo",
        pipeline_name=pipeline_name,
        branch=branch,
        module=Demo.__module__ if module is None else module,
        name=name,
    )


def test_load_state_wraps_shared_pipeline_status(tmp_path: Path) -> None:
    output_base = tmp_path / "demo" / "out"
    run(Demo, Config(), cli_out=output_base)

    state = load_state(_entry(output_base / "main"))

    assert isinstance(state.pipeline_status, PipelineStatus)
    assert state.status == "hit"
    assert state.complete is True
    assert [stage.name for stage in state.stages] == ["sample", "summary"]
    assert all(stage.status == "hit" for stage in state.stages)


def test_load_state_passes_shared_command_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _KeyingSession()
    seen = []

    def fake_collect(context, *, session=None, **kwargs):
        seen.append(session)
        return PipelineStatus(
            pipeline="Demo",
            branch="main",
            output_root=context.output_root,
            stages=(),
        )

    monkeypatch.setattr("varve.dashboard.state.collect_pipeline_status", fake_collect)
    load_state(_entry(tmp_path / "demo" / "out" / "main"), session)
    assert seen == [session]


@pytest.mark.parametrize(
    ("entry", "phase"),
    [
        (
            PipelineEntry(
                output_root=Path("/tmp/bad"),
                pipeline_id="bad",
                pipeline_name=None,
                branch="main",
                manifest_error="bad json",
            ),
            "manifest",
        ),
        (_entry(Path("/tmp/import"), module="varve.no_such_module"), "import"),
        (_entry(Path("/tmp/resolve"), branch="missing"), "resolve"),
    ],
)
def test_load_state_reports_pre_evaluation_error_phases(entry: PipelineEntry, phase: str) -> None:
    state = load_state(entry)
    assert state.status == "error"
    assert state.complete is False
    assert state.error is not None
    assert state.error.phase == phase
    assert state.pipeline_status is None


def test_load_state_reports_evaluate_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_collect(*args, **kwargs):
        raise RuntimeError("engine failed")

    monkeypatch.setattr("varve.dashboard.state.collect_pipeline_status", fail_collect)
    state = load_state(_entry(tmp_path / "demo" / "out" / "main"))
    assert state.status == "error"
    assert state.error is not None
    assert state.error.phase == "evaluate"
    assert state.error.message == "engine failed"


def test_resolve_module_entry_defaults_to_exact_main_and_lists_modules(tmp_path: Path) -> None:
    entries = [
        _entry(tmp_path / "main", module="pkg.demo.run"),
        _entry(tmp_path / "alt", module="pkg.demo.run", branch="alt"),
        _entry(tmp_path / "other", module="pkg.other.run"),
    ]
    assert resolve_module_entry(entries, "pkg.demo").output_root == tmp_path / "main"
    assert resolve_module_entry(entries, "pkg.demo", branch="alt").output_root == tmp_path / "alt"
    with pytest.raises(ValueError, match=r"Available modules: pkg.demo, pkg.other"):
        resolve_module_entry(entries, "pkg.missing")


def test_resolve_module_entry_accepts_package_selector_for_main_module(tmp_path: Path) -> None:
    entries = [
        _entry(tmp_path / "main", module="pkg.demo.__main__"),
        _entry(tmp_path / "other", module="pkg.other.__main__"),
    ]

    assert resolve_module_entry(entries, "pkg.demo").output_root == tmp_path / "main"
    assert resolve_module_entry(entries, "pkg.demo.__main__").output_root == tmp_path / "main"
    with pytest.raises(ValueError, match=r"Available modules: pkg.demo, pkg.other"):
        resolve_module_entry(entries, "pkg.missing")


def test_resolve_module_entry_prefers_a_selector_over_a_persisted_module(tmp_path: Path) -> None:
    entries = [
        _entry(tmp_path / "declared", module="pkg.impl.run", name="pkg.demo"),
        _entry(tmp_path / "persisted", module="pkg.demo"),
    ]

    assert resolve_module_entry(entries, "pkg.demo").output_root == tmp_path / "declared"
    assert resolve_module_entry(entries, "pkg").output_root == tmp_path / "persisted"
    assert resolve_module_entry(entries, "pkg.impl.run").output_root == tmp_path / "declared"


def test_resolve_module_entry_accepts_an_unambiguous_selector_suffix(tmp_path: Path) -> None:
    entries = [
        _entry(tmp_path / "main", module="studies.exp.dataset_audit.label_renderability.run"),
        _entry(tmp_path / "other", module="studies.exp.metric_eval.benchmark_misjudgment.run"),
    ]

    assert resolve_module_entry(entries, "label_renderability").output_root == tmp_path / "main"
    assert (
        resolve_module_entry(entries, "dataset_audit.label_renderability").output_root
        == tmp_path / "main"
    )


def test_resolve_module_entry_rejects_an_ambiguous_selector_suffix(tmp_path: Path) -> None:
    entries = [
        _entry(tmp_path / "first", module="studies.exp.dataset_audit.shared.run"),
        _entry(tmp_path / "second", module="studies.exp.metric_eval.shared.run"),
    ]

    with pytest.raises(ValueError, match="Ambiguous module"):
        resolve_module_entry(entries, "shared")


def test_resolve_module_entry_reports_every_ambiguous_candidate(tmp_path: Path) -> None:
    entries = [
        _entry(tmp_path / "first", module="pkg.demo"),
        _entry(tmp_path / "second", module="pkg.demo"),
    ]
    with pytest.raises(ValueError) as exc_info:
        resolve_module_entry(entries, "pkg.demo")
    message = str(exc_info.value)
    assert "Ambiguous module" in message
    assert "class=Demo" in message
    assert str(tmp_path / "first") in message
    assert str(tmp_path / "second") in message


def test_structure_resolution_deduplicates_branches_but_rejects_classes(
    tmp_path: Path,
) -> None:
    entries = [
        _entry(tmp_path / "main", module=Demo.__module__),
        _entry(tmp_path / "alt", module=Demo.__module__, branch="alt"),
    ]
    pipeline = resolve_structure_pipeline(entries, Demo.__module__)
    assert pipeline is Demo

    entries.append(
        _entry(
            tmp_path / "other",
            module=Demo.__module__,
            pipeline_name="OtherDemo",
            branch="other",
        )
    )
    with pytest.raises(ValueError, match="Ambiguous module"):
        resolve_structure_pipeline(entries, Demo.__module__)


def test_structure_resolution_accepts_package_selector_for_main_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _entry(tmp_path / "main", module=f"{Demo.__module__}.__main__")
    monkeypatch.setattr("varve.dashboard.state.import_entry_pipeline", lambda candidate: Demo)

    assert resolve_structure_pipeline([entry], Demo.__module__) is Demo
