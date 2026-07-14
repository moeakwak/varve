from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel
from rich.console import Console

from varve import Axis, Ctx, Pipeline, matrix, stage
from varve.cli.plan import (
    build_plan_graph,
    format_group_progress,
    render_plan,
    wrap_stage_name,
)
from varve.engine.runner import run
from varve.engine.state import SourceReviewState
from varve.status import (
    CellCoordinate,
    PipelineStatus,
    StageStatus,
    StageStatusGroup,
)


class Config(BaseModel):
    pass


ITEM = Axis("item", ["0", "1", "2", "3"])


class TopologyPipeline(Pipeline):
    Config = Config

    @stage(produces="pairs.txt")
    def prepare_pairs(self, ctx: Ctx) -> None:
        (ctx.out / "pairs.txt").write_text("pairs", encoding="utf-8")

    @stage(needs="prepare_pairs", produces="extract.txt")
    def extract(self, ctx: Ctx) -> None:
        (ctx.out / "extract.txt").write_text("extract", encoding="utf-8")

    @matrix(ITEM)
    @stage(needs="extract", produces="official.txt")
    def official(self, ctx: Ctx, *, item: str) -> None:
        ctx.cell_out.mkdir(parents=True, exist_ok=True)
        (ctx.cell_out / "official.txt").write_text(item, encoding="utf-8")

    @stage(needs="extract", produces="text.txt")
    def text_arm(self, ctx: Ctx) -> None:
        (ctx.out / "text.txt").write_text("text", encoding="utf-8")

    @matrix(ITEM)
    @stage(needs="extract", produces="render.txt")
    def render_batches(self, ctx: Ctx, *, item: str) -> None:
        ctx.cell_out.mkdir(parents=True, exist_ok=True)
        (ctx.cell_out / "render.txt").write_text(item, encoding="utf-8")

    @stage(needs=["official", "text_arm", "render_batches"], produces="final.txt")
    def finalize_outputs(self, ctx: Ctx) -> None:
        (ctx.out / "final.txt").write_text("final", encoding="utf-8")


def _cell(
    name: str,
    base_name: str,
    *,
    status: str = "needs-run",
    logical_needs: tuple[str, ...] = (),
    cell: tuple[CellCoordinate, ...] = (),
    batch_progress: tuple[int, int] | None = None,
) -> StageStatus:
    return StageStatus(
        name=name,
        base_name=base_name,
        cell=cell,
        needs=(),
        logical_needs=logical_needs,
        status=status,  # type: ignore[arg-type]
        reason=status,
        summary_reason=status,
        execution_reason=status,
        source_review=SourceReviewState("not-applicable"),
        duration=None,
        committed_at=None,
        decision_key=None,
        stored_key=None,
        key_inputs=None,
        source_changes={},
        unavailable_reason=None,
        batch_progress=batch_progress,
    )


def test_wrap_stage_name_splits_and_truncates() -> None:
    assert wrap_stage_name("short") == ("short",)
    assert wrap_stage_name("render_oracle_comparison_batches_x") == (
        "render_oracle_comparison_",
        "batches_x",
    )
    long = "a" * 20 + "_" + "b" * 40
    lines = wrap_stage_name(long)
    assert lines[0] == "aaaaaaaaaaaaaaaaaaaa_"
    assert len(lines[1]) <= 32
    assert "…" in lines[1]
    assert lines[1].startswith("b")
    assert lines[1].endswith("b")


def _topology_status() -> PipelineStatus:
    cells = (
        _cell("prepare_pairs", "prepare_pairs", status="hit"),
        _cell("extract", "extract", status="hit", logical_needs=("prepare_pairs",)),
        _cell(
            "official@item=0",
            "official",
            status="hit",
            logical_needs=("extract",),
            cell=(CellCoordinate("item", "0"),),
        ),
        _cell(
            "official@item=1",
            "official",
            status="needs-run",
            logical_needs=("extract",),
            cell=(CellCoordinate("item", "1"),),
        ),
        _cell("text_arm", "text_arm", status="hit", logical_needs=("extract",)),
        _cell(
            "render_batches@item=0",
            "render_batches",
            status="hit",
            logical_needs=("extract",),
            cell=(CellCoordinate("item", "0"),),
        ),
        _cell(
            "render_batches@item=1",
            "render_batches",
            status="needs-run",
            logical_needs=("extract",),
            cell=(CellCoordinate("item", "1"),),
        ),
        _cell(
            "render_batches@item=2",
            "render_batches",
            status="needs-run",
            logical_needs=("extract",),
            cell=(CellCoordinate("item", "2"),),
        ),
        _cell(
            "finalize_outputs",
            "finalize_outputs",
            status="needs-run",
            logical_needs=("official", "text_arm", "render_batches"),
        ),
        _cell(
            "aaaaaaaaaaaaaaaaaaaa_" + "b" * 40,
            "aaaaaaaaaaaaaaaaaaaa_" + "b" * 40,
            status="needs-run",
        ),
    )
    return PipelineStatus(
        pipeline="TopologyPipeline",
        branch="main",
        output_root=Path("out"),
        stages=cells,
    )


def test_plan_graph_preserves_chain_fork_and_join_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    sentinel = object()

    def capture_graph(nodes, edges, **options):
        captured["nodes"] = nodes
        captured["edges"] = tuple(edges)
        captured["options"] = options
        return sentinel

    monkeypatch.setattr("varve.cli.plan.ConsoleGraph", capture_graph)
    result = build_plan_graph(
        _topology_status(),
        console=Console(width=120, color_system=None),
    )

    assert result is sentinel
    assert {(upstream, downstream) for upstream, downstream, _attrs in captured["edges"]} == {
        ("prepare_pairs", "extract"),
        ("extract", "official"),
        ("extract", "text_arm"),
        ("extract", "render_batches"),
        ("official", "finalize_outputs"),
        ("text_arm", "finalize_outputs"),
        ("render_batches", "finalize_outputs"),
    }


def test_plan_renderer_covers_matrix_progress_and_long_names() -> None:
    status = _topology_status()
    console = Console(record=True, width=120, force_terminal=True)
    render_plan(console, status)
    output = console.export_text()

    assert "Plan · main" in output
    assert "prepare_pairs  ✓" in output
    assert "extract" in output
    assert "official" in output
    assert "text_arm" in output
    assert "render_batches" in output
    assert "finalize_outputs" in output
    assert "1/2 cells" in output
    assert "1/3 cells" in output
    assert "pending" in output
    assert "official@item=" not in output
    assert "render_batches@item=" not in output
    assert "…" in output


def test_format_group_progress_matrix_and_batch() -> None:
    matrix_group = StageStatusGroup(
        base_name="official",
        cells=(
            _cell(
                "official@item=0",
                "official",
                status="hit",
                cell=(CellCoordinate("item", "0"),),
            ),
            _cell(
                "official@item=1",
                "official",
                status="needs-run",
                cell=(CellCoordinate("item", "1"),),
            ),
        ),
        review=SourceReviewState("not-applicable"),
    )
    assert format_group_progress(matrix_group).plain == "1/2 cells"

    batch_group = StageStatusGroup(
        base_name="work",
        cells=(
            _cell(
                "work",
                "work",
                status="resume",
                batch_progress=(3, 12),
            ),
        ),
        review=SourceReviewState("not-applicable"),
    )
    assert format_group_progress(batch_group).plain == "3/12 batches"

    failed_batch_group = StageStatusGroup(
        base_name="work",
        cells=(
            _cell(
                "work",
                "work",
                status="failed",
                batch_progress=(3, 12),
            ),
        ),
        review=SourceReviewState("not-applicable"),
    )
    assert format_group_progress(failed_batch_group).plain == "3/12 batches · ✕ failed"

    review_batch_group = StageStatusGroup(
        base_name="work",
        cells=(
            _cell(
                "work",
                "work",
                status="needs-review",
                batch_progress=(3, 12),
            ),
        ),
        review=SourceReviewState("changed"),
    )
    assert format_group_progress(review_batch_group).plain == ("3/12 batches · ! needs-review")


def test_plan_is_read_only_and_does_not_run_stage_bodies(tmp_path: Path) -> None:
    executed: list[str] = []

    class ReadOnly(Pipeline):
        Config = Config

        @stage(produces="sample.txt")
        def sample(self, ctx: Ctx) -> None:
            executed.append("sample")
            (ctx.out / "sample.txt").write_text("sample", encoding="utf-8")

    before = list((tmp_path / "main").rglob("*")) if (tmp_path / "main").exists() else []
    assert ReadOnly.cli(["plan", "--out", str(tmp_path)]) == 0
    after = list((tmp_path / "main").rglob("*")) if (tmp_path / "main").exists() else []

    assert executed == []
    assert before == after
    assert not (tmp_path / "main" / ".varve").exists()


class PlanArgs(BaseModel):
    workers: int = 1


class PlanDemo(Pipeline):
    Config = Config
    Args = PlanArgs

    @stage(produces="sample.txt")
    def sample(self, ctx: Ctx) -> None:
        (ctx.out / "sample.txt").write_text(str(ctx.args.workers), encoding="utf-8")


def test_plan_cli_registers_args_and_rehash(tmp_path: Path, capsys) -> None:
    output_base = tmp_path / "plan_demo"
    run(PlanDemo, Config(), args=PlanArgs(workers=2), cli_out=output_base)
    capsys.readouterr()

    assert (
        PlanDemo.cli(
            ["plan", "--out", str(output_base), "--rehash", "--workers", "2", "--only", "sample"]
        )
        == 0
    )
    generated = capsys.readouterr().out
    assert "Plan · main" in generated
    assert "sample" in generated
    assert "✓" in generated
    assert "sample@item=" not in generated
