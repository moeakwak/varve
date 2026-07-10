"""Tests for structured and Rich pipeline details."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from pydantic import BaseModel
from rich.console import Console

from varve import Pipeline, stage
from varve.cli.details import render_details
from varve.details import PipelineDetails, StageDetails, collect_pipeline_details
from varve.engine.runner import run
from varve.keying.dependencies import DependencyEdge, DependencyNode, SourceDependencies


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


def test_collect_details_counts_unique_dependencies(tmp_path: Path) -> None:
    details = collect_pipeline_details(
        SharedDependencyPipeline,
        Config(),
        args=SharedDependencyPipeline.Args(),
        out=tmp_path / "main",
        branch="main",
    )

    stage_details = details.stages[0]
    assert stage_details.direct_count == 2
    assert stage_details.total_count == 4
    assert stage_details.broad_count == 1


def test_collect_details_uses_decision_and_stored_key_names(tmp_path: Path) -> None:
    run(SharedDependencyPipeline, Config(), cli_out=tmp_path)

    stage_details = collect_pipeline_details(
        SharedDependencyPipeline,
        Config(),
        args=SharedDependencyPipeline.Args(),
        out=tmp_path / "main",
        branch="main",
    ).stages[0]

    assert stage_details.decision_key is not None
    assert stage_details.stored_key is not None
    assert stage_details.status == "hit"


def test_collect_details_marks_inputs_unavailable_after_missing_upstream(
    tmp_path: Path,
) -> None:
    details = collect_pipeline_details(
        DownstreamPipeline,
        Config(),
        args=DownstreamPipeline.Args(),
        out=tmp_path / "main",
        branch="main",
    )

    assert [stage.name for stage in details.stages] == ["prepare", "finish"]
    downstream = details.stages[1]
    assert downstream.decision_key is None
    assert downstream.key_inputs is None
    assert downstream.unavailable_reason == "upstream prepare has no success record"


def test_collect_details_filters_after_whole_pipeline_probe(tmp_path: Path) -> None:
    details = collect_pipeline_details(
        DownstreamPipeline,
        Config(),
        args=DownstreamPipeline.Args(),
        out=tmp_path / "main",
        branch="experiment",
        stage="finish",
    )

    assert details.pipeline == "DownstreamPipeline"
    assert details.branch == "experiment"
    assert [stage.name for stage in details.stages] == ["finish"]


def test_collect_details_rejects_unknown_stage_before_probing(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Unknown varve stage: missing"):
        collect_pipeline_details(
            DownstreamPipeline,
            Config(),
            args=DownstreamPipeline.Args(),
            out=tmp_path / "main",
            branch="main",
            stage="missing",
        )


@pytest.fixture
def pipeline_details(tmp_path: Path):
    return collect_pipeline_details(
        SharedDependencyPipeline,
        Config(),
        args=SharedDependencyPipeline.Args(),
        out=tmp_path / "main",
        branch="main",
    )


def render_to_text(details, *, stage: str | None, depth: int | None) -> str:
    buffer = StringIO()
    console = Console(file=buffer, width=100, color_system=None)
    render_details(console, details, stage=stage, depth=depth)
    return buffer.getvalue()


def test_summary_is_folded_and_lists_every_stage(pipeline_details) -> None:
    output = render_to_text(pipeline_details, stage=None, depth=0)
    assert "STAGE" in output
    assert "SOURCE DEPENDENCIES" in output
    assert "normalize" in output
    assert "2 direct · 4 total · 1 broad" in output
    assert "Dependencies are folded." in output
    assert "test_details.shared_helper" not in output


def test_stage_default_folds_transitive_dependencies(pipeline_details) -> None:
    output = render_to_text(pipeline_details, stage="normalize", depth=0)
    assert "Source dependencies · folded" in output
    assert "… 2 transitive dependencies folded" in output
    assert "Decision key" in output
    assert "Stored key" in output


def test_stage_expand_and_all_show_progressively_more_nodes(pipeline_details) -> None:
    folded = render_to_text(pipeline_details, stage="normalize", depth=0)
    expanded = render_to_text(pipeline_details, stage="normalize", depth=1)
    full = render_to_text(pipeline_details, stage="normalize", depth=None)
    assert len(folded) < len(expanded) < len(full)
    assert "↳ test_details.shared_helper already shown" in full
    repeated = full.index("↳ test_details.shared_helper already shown")
    direct_reason = full.index("global referenced by test_details.direct_helper")
    assert repeated < direct_reason
    assert "[inferred]" in full


def test_stage_with_missing_upstream_explains_unavailable_inputs(tmp_path: Path) -> None:
    details = collect_pipeline_details(
        DownstreamPipeline,
        Config(),
        args=DownstreamPipeline.Args(),
        out=tmp_path / "main",
        branch="main",
        stage="finish",
    )
    output = render_to_text(details, stage="finish", depth=0)
    assert "Decision key" in output
    assert "unavailable" in output
    assert "Key inputs unavailable: upstream prepare has no success record" in output
    assert "Auto dependencies are best effort." in output


def test_folded_tree_uses_reference_instead_of_zero_hidden_count() -> None:
    first = DependencyNode(
        identity="function:test.first",
        kind="function",
        qualified_name="test.first",
        digest="sha256:first",
        origin="inferred",
        scope=None,
        source_path=None,
        source_line=None,
    )
    second = DependencyNode(
        identity="function:test.second",
        kind="function",
        qualified_name="test.second",
        digest="sha256:second",
        origin="explicit",
        scope=None,
        source_path=None,
        source_line=None,
    )
    source = SourceDependencies(
        components={},
        nodes={first.identity: first, second.identity: second},
        edges=(
            DependencyEdge("stage", first.identity, "first stage reason"),
            DependencyEdge("stage", second.identity, "declared by uses"),
            DependencyEdge(second.identity, first.identity, "second references first"),
        ),
        direct=(first.identity, second.identity),
    )
    stage = StageDetails(
        name="sample",
        kind="single",
        needs=(),
        status="no-cache",
        reason="no cache",
        decision_key=None,
        stored_key=None,
        key_inputs=None,
        source_dependencies=source,
        unavailable_reason="test fixture",
    )
    details = PipelineDetails(
        pipeline="Demo",
        branch="main",
        output_root=Path("out/main"),
        stages=(stage,),
    )

    output = render_to_text(details, stage="sample", depth=0)

    assert "… 0 transitive dependencies folded" not in output
    assert "↳ test.first already shown" in output
    assert "second references first" in output
    assert "[explicit]" in output
