from __future__ import annotations

from enum import Enum
from pathlib import Path

import pytest
from pydantic import BaseModel

from varve import Axis, Ctx, Pipeline, batch_stage, matrix, stage
from varve.branch_config import resolve_branch
from varve.engine.runner import run, selected_stages
from varve.matrix import build_graph
from varve.store.store import Store


class Config(BaseModel):
    pass


BENCH = Axis("bench", ["a", "b"])


class Model(str, Enum):
    SMALL = "small"
    LARGE = "large"


MODEL = Axis("model", list(Model))


class MatrixPipeline(Pipeline):
    Config = Config

    @matrix(BENCH)
    @stage(produces="source.txt")
    def source(self, ctx: Ctx, *, bench: str) -> None:
        ctx.cell_out.mkdir(parents=True, exist_ok=True)
        (ctx.cell_out / "source.txt").write_text(bench)

    @matrix(BENCH, MODEL)
    @stage(needs=["source"], produces="score.txt")
    def score(self, ctx: Ctx, *, bench: str, model: Model) -> None:
        assert ctx.cell.bench == bench
        ctx.cell_out.mkdir(parents=True, exist_ok=True)
        (ctx.cell_out / "score.txt").write_text(f"{ctx.input('source').read_text()}:{model.value}")

    @stage(needs=["score"], produces="all.txt")
    def finish(self, ctx: Ctx) -> None:
        (ctx.out / "all.txt").write_text(
            "\n".join(path.read_text() for path in ctx.inputs("score"))
        )


def test_graph_expands_and_wires_shared_axes_in_declaration_order() -> None:
    graph = build_graph(MatrixPipeline)

    assert graph.topo_order() == [
        "source@bench=a",
        "source@bench=b",
        "score@bench=a,model=small",
        "score@bench=a,model=large",
        "score@bench=b,model=small",
        "score@bench=b,model=large",
        "finish",
    ]
    assert graph.stages["score@bench=b,model=large"].needs == ("source@bench=b",)
    assert graph.stages["finish"].needs == graph.base_cells["score"]


def test_branch_axis_domain_limits_expansion_without_reordering() -> None:
    graph = build_graph(MatrixPipeline, {"model": ["large"], "bench": ["b", "a"]})

    assert graph.base_cells["score"] == (
        "score@bench=a,model=large",
        "score@bench=b,model=large",
    )


def test_matrix_run_injects_coordinates_fans_in_and_isolates_outputs(tmp_path: Path) -> None:
    outcomes = run(MatrixPipeline, Config(), cli_out=tmp_path)
    out = tmp_path / "main"

    assert [outcome.stage for outcome in outcomes] == build_graph(MatrixPipeline).topo_order()
    assert (out / "score@bench=a,model=small" / "score.txt").read_text() == "a:small"
    assert (out / "all.txt").read_text().splitlines() == [
        "a:small",
        "a:large",
        "b:small",
        "b:large",
    ]


def test_matrix_absolute_output_must_stay_in_cell_directory(tmp_path: Path) -> None:
    class Bad(Pipeline):
        Config = Config

        @matrix(BENCH)
        @stage(produces=lambda ctx: ctx.out / "shared.txt")
        def write(self, ctx: Ctx, *, bench: str) -> None:
            (ctx.out / "shared.txt").write_text(bench)

    with pytest.raises(ValueError, match="inside the output root"):
        run(Bad, Config(), cli_out=tmp_path)


def test_matrix_static_output_escape_is_rejected_before_body(tmp_path: Path) -> None:
    class Bad(Pipeline):
        Config = Config

        @matrix(BENCH)
        @stage(produces="../shared.txt")
        def write(self, ctx: Ctx, *, bench: str) -> None:
            (ctx.out / "body-ran.txt").write_text(bench)

    with pytest.raises(ValueError, match="inside the output root"):
        run(Bad, Config(), cli_out=tmp_path)
    assert not (tmp_path / "main" / "body-ran.txt").exists()


def test_slice_selects_matching_cells_and_their_aligned_upstreams() -> None:
    graph = build_graph(MatrixPipeline)

    selected = selected_stages(graph, slices=["model=small", "bench=b"])

    assert selected == {
        "source@bench=b",
        "score@bench=b,model=small",
    }


def test_multi_axis_slice_does_not_seed_partial_axis_fan_in() -> None:
    class Sliced(Pipeline):
        Config = Config

        @matrix(BENCH, MODEL)
        @stage()
        def score(self, ctx: Ctx, *, bench: str, model: Model) -> None:
            pass

        @matrix(BENCH)
        @stage(needs="score")
        def summary(self, ctx: Ctx, *, bench: str) -> None:
            pass

    selected = selected_stages(build_graph(Sliced), slices=["model=small", "bench=b"])
    assert selected == {"score@bench=b,model=small"}


def test_only_with_slice_keeps_aligned_upstream_closure() -> None:
    selected = selected_stages(build_graph(MatrixPipeline), only="score", slices=["model=small"])
    assert selected == {
        "source@bench=a",
        "source@bench=b",
        "score@bench=a,model=small",
        "score@bench=b,model=small",
    }


def test_temporary_branch_snapshots_axes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    yaml_path = tmp_path / "varve.yaml"
    yaml_path.write_text(
        "main:\n  axes:\n    model: [small]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(MatrixPipeline, "varve_config_path", classmethod(lambda cls: yaml_path))
    resolved = resolve_branch(
        MatrixPipeline,
        branch="main",
        override_json="{}",
        cli_out=tmp_path,
    )

    run(
        MatrixPipeline,
        resolved.config,
        cli_out=resolved.output_base,
        branch=resolved.branch,
        is_temporary=True,
        temporary_config=resolved.temporary_config,
        axes=resolved.axes,
        temporary_axes=resolved.temporary_axes,
    )
    manifest = Store(tmp_path / ".tmp" / resolved.branch).read_manifest()
    assert manifest is not None
    assert manifest.temporary_axes == {"bench": ["a", "b"], "model": ["small"]}


def test_yaml_temporary_branch_snapshots_and_recovers_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class TokenConfig(BaseModel):
        token: str = "default"

    class Temporary(Pipeline):
        Config = TokenConfig

        @stage(produces="token.txt")
        def write(self, ctx: Ctx) -> None:
            (ctx.out / "token.txt").write_text(ctx.config.token)

    yaml_path = tmp_path / "varve.yaml"
    yaml_path.write_text(
        "smoke:\n  is_temporary: true\n  config:\n    token: first\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Temporary, "varve_config_path", classmethod(lambda cls: yaml_path))
    resolved = resolve_branch(Temporary, branch="smoke", override_json=None, cli_out=tmp_path)
    run(
        Temporary,
        resolved.config,
        cli_out=tmp_path,
        branch="smoke",
        is_temporary=True,
        temporary_config=resolved.temporary_config,
        temporary_axes=resolved.temporary_axes,
    )

    yaml_path.write_text(
        "smoke:\n  is_temporary: true\n  config:\n    token: changed\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="different config"):
        resolve_branch(Temporary, branch="smoke", override_json=None, cli_out=tmp_path)

    yaml_path.unlink()
    recovered = resolve_branch(Temporary, branch="smoke", override_json=None, cli_out=tmp_path)
    assert recovered.config.token == "first"


def test_status_base_name_renders_all_cells(tmp_path: Path, capsys) -> None:
    run(MatrixPipeline, Config(), cli_out=tmp_path)

    assert MatrixPipeline.cli(["status", "score", "--out", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "score@bench=a,model=small" in output
    assert "score@bench=b,model=large" in output


def test_same_axis_name_on_distinct_objects_is_rejected() -> None:
    other = Axis("bench", ["x"])

    class Bad(Pipeline):
        Config = Config

        @matrix(BENCH)
        @stage()
        def left(self, ctx: Ctx, *, bench: str) -> None:
            pass

        @matrix(other)
        @stage()
        def right(self, ctx: Ctx, *, bench: str) -> None:
            pass

    with pytest.raises(ValueError, match="different Axis objects"):
        build_graph(Bad)


def test_matrix_requires_exact_keyword_only_coordinate_parameters() -> None:
    class Bad(Pipeline):
        Config = Config

        @matrix(BENCH)
        @stage()
        def write(self, ctx: Ctx) -> None:
            pass

    with pytest.raises(TypeError, match="coordinate parameters"):
        build_graph(Bad)


def test_axis_validation() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        Axis("seed", [1, 1])
    with pytest.raises(ValueError, match="ids must match"):
        Axis("model", ["has spaces"])


def test_plain_enum_uses_member_name_as_id() -> None:
    class Color(Enum):
        RED = "red"

    assert Axis("color", [Color.RED]).ids == ("RED",)


def test_fan_in_order_change_invalidates_aggregate(tmp_path: Path) -> None:
    run(MatrixPipeline, Config(), cli_out=tmp_path)
    original_values = MODEL.values
    original_ids = MODEL.ids
    original_by_id = MODEL._by_id
    try:
        MODEL.values = tuple(reversed(MODEL.values))
        MODEL.ids = tuple(reversed(MODEL.ids))
        MODEL._by_id = dict(zip(MODEL.ids, MODEL.values, strict=True))
        outcomes = run(MatrixPipeline, Config(), cli_out=tmp_path)
    finally:
        MODEL.values = original_values
        MODEL.ids = original_ids
        MODEL._by_id = original_by_id
    assert outcomes[-1].stage == "finish"
    assert outcomes[-1].status == "stale"


def test_only_rejects_stale_external_upstream(tmp_path: Path) -> None:
    class TokenConfig(BaseModel):
        token: str

    class Only(Pipeline):
        Config = TokenConfig

        @stage(produces="source.txt")
        def source(self, ctx: Ctx) -> None:
            (ctx.out / "source.txt").write_text(ctx.config.token)

        @stage(needs="source", produces="result.txt")
        def result(self, ctx: Ctx) -> None:
            (ctx.out / "result.txt").write_text(ctx.input("source").read_text())

    run(Only, TokenConfig(token="old"), cli_out=tmp_path)
    with pytest.raises(ValueError, match="Upstream stage is not current"):
        run(Only, TokenConfig(token="new"), cli_out=tmp_path, only="result")


def test_matrix_batch_outputs_are_cell_isolated(tmp_path: Path) -> None:
    class Batched(Pipeline):
        Config = Config

        @matrix(BENCH)
        @batch_stage()
        async def parts(self, ctx: Ctx, *, bench: str):
            async for index, value in ctx.resume([bench], progress=False):
                ctx.cell_out.mkdir(parents=True, exist_ok=True)
                path = ctx.cell_out / f"{index}.txt"
                path.write_text(value)
                yield path

    run(Batched, Config(), cli_out=tmp_path)
    assert (tmp_path / "main" / "parts@bench=a" / "0.txt").read_text() == "a"
    assert (tmp_path / "main" / "parts@bench=b" / "0.txt").read_text() == "b"
