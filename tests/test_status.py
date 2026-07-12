"""Tests for structured and Rich pipeline status."""

from __future__ import annotations

from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest
from pydantic import BaseModel
from rich.console import Console

from varve import Pipeline, stage
from varve.cli.status import reason_text, render_status
from varve.engine.runner import run
from varve.status import (
    CellCoordinate,
    collect_pipeline_status,
    source_component_changes,
)
from varve.store.store import Store


class Config(BaseModel):
    profile: str = "default"


def leaf_helper(value: str) -> str:
    return value.upper()


def shared_helper(value: str) -> str:
    return leaf_helper(value)


def direct_helper(value: str) -> str:
    return shared_helper(value)


class Renderer:
    def render(self, value: str) -> str:
        return shared_helper(value)


class SharedDependencyPipeline(Pipeline):
    Config = Config

    @stage(produces="result.txt")
    def normalize(self, ctx):
        value = direct_helper(ctx.config.profile)
        rendered = Renderer().render(value)
        (ctx.out / "result.txt").write_text(rendered, encoding="utf-8")


class DownstreamPipeline(Pipeline):
    Config = Config

    @stage(produces="prepare.txt")
    def prepare(self, ctx):
        (ctx.out / "prepare.txt").write_text(ctx.config.profile, encoding="utf-8")

    @stage(needs="prepare", produces="finish.txt")
    def finish(self, ctx):
        value = ctx.input("prepare").read_text(encoding="utf-8")
        (ctx.out / "finish.txt").write_text(value, encoding="utf-8")


def test_collect_status_reads_previous_record(tmp_path: Path) -> None:
    run(SharedDependencyPipeline, Config(), cli_out=tmp_path)
    store = Store(tmp_path / "main")
    previous = store.read_success("normalize")
    assert previous is not None
    store.write_success(previous.model_copy(update={"elapsed": 1.25}))

    stage_status = collect_pipeline_status(
        SharedDependencyPipeline,
        Config(),
        args=SharedDependencyPipeline.Args(),
        out=tmp_path / "main",
        branch="main",
    ).stages[0]

    assert stage_status.status == "hit"
    assert stage_status.decision_key is not None
    assert stage_status.stored_key is not None
    assert stage_status.duration == 1.25
    assert stage_status.source_changes == {}


def test_collect_status_keeps_previous_duration_when_upstream_record_is_missing(
    tmp_path: Path,
) -> None:
    run(DownstreamPipeline, Config(), cli_out=tmp_path)
    store = Store(tmp_path / "main")
    (store.root / "stages" / "prepare.json").unlink()

    downstream = collect_pipeline_status(
        DownstreamPipeline,
        Config(),
        args=DownstreamPipeline.Args(),
        out=tmp_path / "main",
        branch="main",
        stage="finish",
    ).stages[0]

    assert downstream.status == "needs-run"
    assert downstream.stored_key is not None
    assert downstream.duration is not None
    assert downstream.source_changes == {}


def test_source_component_changes_classifies_changed_added_and_removed() -> None:
    assert source_component_changes(
        {"stage": "old", "auto.function.old": "same", "auto.module.removed": "old"},
        {"stage": "new", "auto.function.old": "same", "auto.value.added": "new"},
    ) == {
        "auto.module.removed": "removed",
        "auto.value.added": "added",
        "stage": "changed",
    }


def test_collect_status_marks_inputs_unavailable_after_missing_upstream(
    tmp_path: Path,
) -> None:
    status = collect_pipeline_status(
        DownstreamPipeline,
        Config(),
        args=DownstreamPipeline.Args(),
        out=tmp_path / "main",
        branch="main",
    )

    assert [stage.name for stage in status.stages] == ["prepare", "finish"]
    downstream = status.stages[1]
    assert downstream.decision_key is None
    assert downstream.key_inputs is None
    assert downstream.unavailable_reason == "upstream prepare has no success record"


def test_collect_status_filters_after_whole_pipeline_probe(tmp_path: Path) -> None:
    status = collect_pipeline_status(
        DownstreamPipeline,
        Config(),
        args=DownstreamPipeline.Args(),
        out=tmp_path / "main",
        branch="experiment",
        stage="finish",
    )

    assert status.pipeline == "DownstreamPipeline"
    assert status.branch == "experiment"
    assert [stage.name for stage in status.stages] == ["finish"]


def test_collect_status_rejects_unknown_stage_before_probing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown varve stage: missing"):
        collect_pipeline_status(
            DownstreamPipeline,
            Config(),
            args=DownstreamPipeline.Args(),
            out=tmp_path / "main",
            branch="main",
            stage="missing",
        )


@pytest.fixture
def pipeline_status(tmp_path: Path):
    return collect_pipeline_status(
        SharedDependencyPipeline,
        Config(),
        args=SharedDependencyPipeline.Args(),
        out=tmp_path / "main",
        branch="main",
    )


def render_to_text(
    status,
    *,
    stage: str | None,
    depth: int | None,
    width: int = 100,
) -> str:
    buffer = StringIO()
    console = Console(file=buffer, width=width, color_system=None)
    view = "summary" if stage is None and depth == 0 else "detail"
    render_status(console, status, view=view, dependency_depth=depth)
    return buffer.getvalue()


def test_reason_highlights_upstream_stage_and_change_keyword() -> None:
    reason = reason_text("upstream 'extract' changed (+ source)")
    spans = {reason.plain[span.start : span.end]: span.style for span in reason.spans}

    assert reason.plain == "upstream extract changed (+ source)"
    assert spans["extract"] == "bold"
    assert spans["changed"] == "yellow"

    source_reason = reason_text("source changed")
    source_spans = {
        source_reason.plain[span.start : span.end]: span.style for span in source_reason.spans
    }
    assert source_spans["changed"] == "yellow"


def test_summary_shows_duration_folds_needs_and_omits_key(pipeline_status) -> None:
    stage = replace(
        pipeline_status.stages[0],
        duration=1.25,
        needs=(
            "extract",
            "text_arm",
            "texform_arm_batches",
            "official_replay",
            "render_oracle_batches",
            "prepare_pairs",
        ),
        logical_needs=(
            "extract",
            "text_arm",
            "texform_arm_batches",
            "official_replay",
            "render_oracle_batches",
            "prepare_pairs",
        ),
    )
    status = replace(pipeline_status, stages=(stage,))
    output = render_to_text(status, stage=None, depth=0, width=160)
    assert "DURATION" in output
    assert "1.25s" in output
    assert "extract, text_arm · +4 more" in output
    assert "confirmed" in output
    assert "test_status.shared_helper" not in output
    assert "KEY" not in output


def test_matrix_group_aggregates_mixed_statuses_and_recorded_durations(
    pipeline_status,
) -> None:
    original = pipeline_status.stages[0]

    def coordinate(value: str) -> tuple[CellCoordinate, ...]:
        return (CellCoordinate(axis="model", value_id=value),)

    cells = (
        replace(
            original,
            name="score@model=a",
            base_name="score",
            cell=coordinate("a"),
            logical_needs=("prepare",),
            status="hit",
            reason="hit",
            summary_reason="hit",
            duration=1.0,
        ),
        replace(
            original,
            name="score@model=b",
            base_name="score",
            cell=coordinate("b"),
            logical_needs=("prepare",),
            status="needs-run",
            reason="inputs-changed",
            summary_reason="inputs-changed",
            duration=2.0,
        ),
        replace(
            original,
            name="score@model=c",
            base_name="score",
            cell=coordinate("c"),
            logical_needs=("prepare",),
            status="failed",
            reason="stage-failed",
            summary_reason="stage-failed",
            duration=None,
        ),
    )
    status = replace(pipeline_status, stages=cells)

    group = status.groups[0]
    assert group.status == "failed"
    assert group.status_counts == (("hit", 1), ("needs-run", 1), ("failed", 1))
    assert group.duration == 3.0
    assert group.recorded_duration_count == 2

    output = render_to_text(status, stage=None, depth=0, width=160)
    assert "1 hit · 1 needs-run · 1 failed" in output
    assert "3.00s · 2/3" in output
    assert "prepare" in output

    buffer = StringIO()
    console = Console(
        file=buffer,
        width=80,
        color_system="standard",
        force_terminal=True,
        no_color=False,
    )
    render_status(console, status, view="summary")
    colored = buffer.getvalue()
    assert "\x1b[31" in colored
    assert "\x1b[32" in colored
    assert "failed" in colored
    assert "hit" in colored


@pytest.mark.parametrize("width", [40, 60, 80])
def test_summary_does_not_ellipsize_core_fields_on_narrow_terminals(
    pipeline_status,
    width: int,
) -> None:
    stage = replace(
        pipeline_status.stages[0],
        name="render_oracle_batches_Ω",
        base_name="render_oracle_batches_Ω",
        status="needs-run",
        duration=1.25,
        needs=(
            "prepare_pairs",
            "official_replay",
            "text_arm",
            "texform_arm_batches",
        ),
        logical_needs=(
            "prepare_pairs",
            "official_replay",
            "text_arm",
            "texform_arm_batches",
        ),
        reason="artifact-missing §",
        summary_reason="artifact-missing §",
    )
    output = render_to_text(
        replace(pipeline_status, stages=(stage,)),
        stage=None,
        depth=0,
        width=width,
    )

    assert "Ω" in output
    assert "§" in output
    assert "…" not in output


def test_stage_detail_shows_keys_and_source_review(pipeline_status) -> None:
    folded = render_to_text(pipeline_status, stage="normalize", depth=0)
    expanded = render_to_text(pipeline_status, stage="normalize", depth=1)
    full = render_to_text(pipeline_status, stage="normalize", depth=None)

    assert "Decision key" in folded
    assert "Stored key" in folded
    assert "Source review" in folded
    assert "Decision key" in expanded
    assert "Stored key" in expanded
    assert "Decision key" in full
    assert "Stored key" in full
    assert folded == expanded == full


def test_pending_review_renders_as_source_changed(pipeline_status) -> None:
    stage = replace(pipeline_status.stages[0], source_review="pending")
    status = replace(pipeline_status, stages=(stage,))

    summary = render_to_text(status, stage=None, depth=0)
    detail = render_to_text(status, stage="normalize", depth=0)

    assert "1 source-changed" in summary
    assert "Source review" in detail
    assert "source-changed" in detail


def test_expanded_stage_preserves_complete_keys_on_narrow_terminals(
    pipeline_status,
) -> None:
    decision_key = f"sha256:{'d' * 64}"
    stored_key = f"sha256:{'s' * 64}"
    stage = replace(
        pipeline_status.stages[0],
        decision_key=decision_key,
        stored_key=stored_key,
    )
    output = render_to_text(
        replace(pipeline_status, stages=(stage,)),
        stage="normalize",
        depth=1,
        width=80,
    )
    compact_output = "".join(output.replace("│", "").split())

    assert decision_key in compact_output
    assert stored_key in compact_output


def test_stage_with_missing_upstream_explains_unavailable_inputs(tmp_path: Path) -> None:
    status = collect_pipeline_status(
        DownstreamPipeline,
        Config(),
        args=DownstreamPipeline.Args(),
        out=tmp_path / "main",
        branch="main",
        stage="finish",
    )
    folded = render_to_text(status, stage="finish", depth=0)
    assert "Key inputs unavailable: upstream prepare has no success record" in folded
    assert "Source review" in folded
