from __future__ import annotations

import logging
from pathlib import Path
from typing import get_type_hints

import pytest
from pydantic import BaseModel
from rich.text import Text

from varve import Axis, Ctx, Pipeline, matrix, stage
from varve.cli import run as cli_run
from varve.cli.run import render_run_outcomes
from varve.engine.run_display import (
    AUTO_COMPACT_MIN_CELLS,
    AUTO_EXPAND_SLOW_SECONDS,
    RunReporter,
    StageOutcome,
    build_run_display_plan,
    format_run_order_marker,
    outcome_rows,
)
from varve.engine.runner import run
from varve.engine.state import (
    ExecutionStatus,
    SourceReviewState,
    aggregate_effective_status,
    aggregate_execution_status,
    effective_reason,
    effective_status,
)
from varve.matrix import build_graph
from varve.models import (
    ArtifactFingerprint,
    KeyComponents,
    ProducedPath,
    SourceFingerprint,
    SourceObservation,
    SuccessRecord,
)
from varve.store.store import Store
from varve.style import VarveStatusHighlighter, make_console


class Config(BaseModel):
    pass


ITEM = Axis("item", [str(index) for index in range(AUTO_COMPACT_MIN_CELLS)])


class LargeMatrix(Pipeline):
    Config = Config

    @matrix(ITEM)
    @stage()
    def work(self, ctx: Ctx, *, item: str) -> None:
        pass


def test_execution_and_effective_statuses_are_orthogonal() -> None:
    assert get_type_hints(StageOutcome)["status"] == ExecutionStatus
    assert SourceReviewState("current", "reuse") == SourceReviewState("current")
    assert aggregate_execution_status(("hit", "failed", "needs-run")) == "failed"
    assert aggregate_effective_status(("error", "needs-review")) == "needs-review"
    assert effective_status("error", SourceReviewState("changed")) == "needs-review"
    assert effective_reason("artifact-missing", SourceReviewState("changed")) == "source-changed"
    assert effective_status("hit", SourceReviewState("changed", "reuse")) == "hit"
    assert effective_reason("artifact-missing", SourceReviewState("changed", "reuse")) == (
        "artifact-missing"
    )
    assert effective_status("failed", SourceReviewState("changed", "invalidate")) == "needs-run"


def _record(stage: str, *, elapsed: float) -> SuccessRecord:
    artifact = ArtifactFingerprint(
        root="artifact", kind="file", manifest=[], fingerprint="artifact"
    )
    return SuccessRecord(
        pipeline="LargeMatrix",
        stage=stage,
        kind="single",
        input_key="key",
        key_components=KeyComponents(config={}, inputs={}, values={}, upstreams={}),
        executed_source=SourceObservation(
            rerun=SourceFingerprint(fingerprint="source", files=[]),
            review=SourceFingerprint(fingerprint="source", files=[]),
        ),
        artifact_fingerprint="artifacts",
        produces=[ProducedPath(path="artifact", kind="file", artifact=artifact)],
        committed_at="now",
        elapsed=elapsed,
    )


def test_auto_display_policy_threshold_flags_and_selected_subset(tmp_path: Path) -> None:
    graph = build_graph(LargeMatrix)
    store = Store(tmp_path)
    all_cells = set(graph.stages)

    assert build_run_display_plan(graph, all_cells, store, mode="auto").groups[0].compact
    assert not build_run_display_plan(graph, all_cells, store, mode="expand").groups[0].compact
    assert (
        build_run_display_plan(graph, {next(iter(all_cells))}, store, mode="compact")
        .groups[0]
        .compact
    )
    assert (
        not build_run_display_plan(
            graph,
            set(tuple(graph.stages)[: AUTO_COMPACT_MIN_CELLS - 1]),
            store,
            mode="auto",
        )
        .groups[0]
        .compact
    )

    sliced = graph.selected(slices=["item=0"])
    assert len(sliced) == 1
    assert not build_run_display_plan(graph, sliced, store, mode="auto").groups[0].compact


def test_direct_run_rejects_invalid_display_mode(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Invalid run display mode"):
        run(LargeMatrix, Config(), cli_out=tmp_path, display_mode="bad")  # type: ignore[arg-type]


def test_auto_display_expands_group_with_known_slow_cell(tmp_path: Path) -> None:
    graph = build_graph(LargeMatrix)
    store = Store(tmp_path)
    store.write_success(_record(graph.base_cells["work"][3], elapsed=AUTO_EXPAND_SLOW_SECONDS))

    plan = build_run_display_plan(graph, set(graph.stages), store, mode="auto")

    assert not plan.groups[0].compact


def test_compact_reporter_finishes_non_contiguous_group_by_count(caplog) -> None:
    graph = build_graph(LargeMatrix)
    plan = build_run_display_plan(
        graph,
        set(graph.stages),
        Store(Path("unused")),
        mode="compact",
    )
    reporter = RunReporter(plan, logging.getLogger("varve"))
    caplog.set_level(logging.INFO, logger="varve")
    stages = graph.base_cells["work"]

    reporter.start(stages[0])
    for stage_name in stages[:-1]:
        reporter.record(plan.outcome(stage_name, "hit", "hit", None))
    assert not any("ran 0" in record.getMessage() for record in caplog.records)

    # Completion depends on the selected-cell count, not adjacency in the
    # caller's event stream.
    reporter.record(plan.outcome(stages[-1], "needs-run", "source-changed", 0.25))
    messages = [record.getMessage() for record in caplog.records]
    assert "[work] start · 8 cells" in messages
    assert "[work] done · 8 cells · 7 hit, 1 needs-run · ran 1 · 0.25s" in messages


def test_compact_reporter_surfaces_new_slow_cell_by_concrete_name(caplog) -> None:
    graph = build_graph(LargeMatrix)
    plan = build_run_display_plan(
        graph,
        set(graph.stages),
        Store(Path("unused")),
        mode="compact",
    )
    reporter = RunReporter(plan, logging.getLogger("varve"))
    caplog.set_level(logging.INFO, logger="varve")
    stage_name = graph.base_cells["work"][0]

    reporter.record(plan.outcome(stage_name, "needs-run", "no-cache", AUTO_EXPAND_SLOW_SECONDS))

    assert f"[{stage_name}] slow · {AUTO_EXPAND_SLOW_SECONDS:.2f}s" in [
        record.getMessage() for record in caplog.records
    ]


def test_compact_runner_aggregates_live_hits_runs_and_outcomes(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="varve")
    first = run(LargeMatrix, Config(), cli_out=tmp_path)
    first_messages = [record.getMessage() for record in caplog.records]

    assert any(message.startswith("Run order: work ") for message in first_messages)
    assert not any("plan:" in message for message in first_messages)
    assert not any(
        "work@item=" in message for message in first_messages if message.startswith("Run order:")
    )
    assert "[work] start · 8 cells" in first_messages
    assert any("8 needs-run · ran 8" in message for message in first_messages)
    assert not any("[work@item=" in message for message in first_messages)
    first_rows = outcome_rows(first)
    assert [(row.stage, row.cells, row.ran) for row in first_rows] == [("work", 8, 8)]

    caplog.clear()
    second = run(LargeMatrix, Config(), cli_out=tmp_path)
    second_messages = [record.getMessage() for record in caplog.records]
    assert any("8 hit · ran 0" in message for message in second_messages)
    second_row = outcome_rows(second)[0]
    assert second_row.status == "8 hit"
    assert second_row.status_counts == (("hit", 8),)
    assert second_row.elapsed is None


def test_compact_cli_outcome_table_has_one_group_row(tmp_path: Path, capsys) -> None:
    outcomes = run(LargeMatrix, Config(), cli_out=tmp_path)

    render_run_outcomes(make_console(), outcomes)

    output = capsys.readouterr().out
    assert "STAGE" in output
    assert "CELLS" in output
    assert "RAN" in output
    assert "work" in output
    assert "8 needs-run" in output
    assert "work@item=" not in output


def test_compact_cli_outcome_table_styles_each_status_token(
    capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    called: list[str] = []

    def tracked_status_text(status: str) -> Text:
        called.append(status)
        return Text(status)

    monkeypatch.setattr(cli_run, "status_text", tracked_status_text)
    outcomes = [
        StageOutcome(
            "work@item=0",
            "hit",
            "hit",
            None,
            display_base="work",
            display_compact=True,
            display_cells=2,
        ),
        StageOutcome(
            "work@item=1",
            "needs-run",
            "source-changed",
            0.5,
            display_base="work",
            display_compact=True,
            display_cells=2,
        ),
    ]

    render_run_outcomes(make_console(), outcomes)

    assert called == ["hit", "needs-run"]
    assert "1 hit, 1 needs-run" in capsys.readouterr().out


def test_expand_runner_keeps_concrete_matrix_lifecycle(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="varve")

    run(LargeMatrix, Config(), cli_out=tmp_path, display_mode="expand")

    messages = [record.getMessage() for record in caplog.records]
    assert any(message.startswith("Run order: work ") for message in messages)
    assert not any(
        "work@item=" in message for message in messages if message.startswith("Run order:")
    )
    assert any("[work@item=0] run" in message for message in messages)
    assert any("[work@item=7] done" in message for message in messages)
    assert not any("[work] start" in message for message in messages)


def test_compact_runner_keeps_concrete_lifecycle_and_keys_at_debug(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.DEBUG, logger="varve")

    run(LargeMatrix, Config(), cli_out=tmp_path, display_mode="compact")

    messages = [record.getMessage() for record in caplog.records]
    assert any("[work@item=0] run" in message for message in messages)
    assert any("[work@item=0] input_key" in message for message in messages)


def test_compact_failure_always_logs_concrete_cell(tmp_path: Path, caplog) -> None:
    class Failing(Pipeline):
        Config = Config

        @matrix(ITEM)
        @stage()
        def work(self, ctx: Ctx, *, item: str) -> None:
            if item == "3":
                raise RuntimeError("planned failure")

    caplog.set_level(logging.INFO, logger="varve")

    with pytest.raises(RuntimeError, match="planned failure"):
        run(Failing, Config(), cli_out=tmp_path, display_mode="compact")

    assert any(
        "[work@item=3] error · planned failure" in record.getMessage() for record in caplog.records
    )


def test_run_order_marker_always_folds_matrix_and_force() -> None:
    assert (
        format_run_order_marker(
            base_name="work",
            stages=("work@item=0", "work@item=1"),
            is_matrix=True,
            forced=False,
            status_by_stage={"work@item=0": "hit", "work@item=1": "needs-run"},
        )
        == "work 1/2"
    )
    assert (
        format_run_order_marker(
            base_name="work",
            stages=("work@item=0", "work@item=1"),
            is_matrix=True,
            forced=True,
            status_by_stage={"work@item=0": "hit", "work@item=1": "hit"},
        )
        == "work run"
    )
    assert (
        format_run_order_marker(
            base_name="prepare",
            stages=("prepare",),
            is_matrix=False,
            forced=False,
            status_by_stage={"prepare": "hit"},
        )
        == "prepare ✓"
    )
    assert (
        format_run_order_marker(
            base_name="build",
            stages=("build",),
            is_matrix=False,
            forced=False,
            status_by_stage={"build": "failed"},
            batch_completed=3,
            batch_total=12,
        )
        == "build 3/12 · ✕ failed"
    )


def test_run_order_highlighter_uses_pending_and_arrow_styles() -> None:
    text = VarveStatusHighlighter()(Text("Run order: prepare run → work 1/2 → finish ✓"))
    spans = {(text.plain[span.start : span.end], span.style) for span in text.spans}

    assert ("run", "varve.run_order_pending") in spans
    assert ("→", "varve.run_order_arrow") in spans
    console = make_console()
    pending_color = console.get_style("varve.run_order_pending").color
    assert pending_color is not None
    assert pending_color.name == "yellow"
    assert console.get_style("varve.run_order_arrow").dim is True


def test_non_matrix_outcome_rows_remain_one_row_per_stage() -> None:
    outcomes = [
        StageOutcome("prepare", "hit", "hit", None),
        StageOutcome("finish", "needs-run", "no-cache", 0.5),
    ]

    rows = outcome_rows(outcomes)

    assert [(row.stage, row.status, row.grouped) for row in rows] == [
        ("prepare", "hit", False),
        ("finish", "needs-run", False),
    ]
