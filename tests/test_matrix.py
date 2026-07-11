from __future__ import annotations

from enum import Enum
from pathlib import Path

import pytest
from pydantic import BaseModel

from varve import Axis, Ctx, Pipeline, batch_stage, matrix, stage
from varve.branch_config import resolve_branch
from varve.cli.clean import clean
from varve.engine import runner as runner_module
from varve.engine.runner import probe_pipeline, run, selected_stages
from varve.keying.keys import content_key
from varve.matrix import build_graph, cell_output_path
from varve.models import BatchRecord, ProducedPath, SuccessRecord
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


def test_cell_output_path_uses_base_stage_and_axis_declaration_order(tmp_path: Path) -> None:
    graph = build_graph(MatrixPipeline)

    assert cell_output_path(tmp_path, graph.stages["score@bench=a,model=small"]) == (
        tmp_path / ".matrix" / "score" / "bench=a" / "model=small"
    )
    assert cell_output_path(tmp_path, graph.stages["finish"]) == tmp_path


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
    assert (
        out / ".matrix" / "score" / "bench=a" / "model=small" / "score.txt"
    ).read_text() == "a:small"
    assert (out / "all.txt").read_text().splitlines() == [
        "a:small",
        "a:large",
        "b:small",
        "b:large",
    ]
    record = Store(out).read_success("score@bench=a,model=small")
    assert record is not None
    assert record.stage == "score@bench=a,model=small"
    assert record.produces == [
        ProducedPath(
            path=".matrix/score/bench=a/model=small/score.txt",
            kind="file",
        )
    ]


def test_matrix_callable_produces_records_the_cell_output_path(tmp_path: Path) -> None:
    axis = Axis("item", ["one"])

    class CallableProduces(Pipeline):
        Config = Config

        @matrix(axis)
        @stage(produces=lambda ctx: ctx.cell_out / "result.txt")
        def write(self, ctx: Ctx, *, item: str) -> None:
            ctx.cell_out.mkdir(parents=True, exist_ok=True)
            (ctx.cell_out / "result.txt").write_text(item)

    run(CallableProduces, Config(), cli_out=tmp_path)
    record = Store(tmp_path / "main").read_success("write@item=one")

    assert record is not None
    assert record.produces == [ProducedPath(path=".matrix/write/item=one/result.txt", kind="file")]


def test_matrix_artifact_existence_and_clean_use_recorded_paths(tmp_path: Path) -> None:
    run(MatrixPipeline, Config(), cli_out=tmp_path)
    out = tmp_path / "main"
    missing = out / ".matrix" / "score" / "bench=a" / "model=small" / "score.txt"
    missing.unlink()

    outcomes = run(MatrixPipeline, Config(), cli_out=tmp_path, only="score")

    assert next(item for item in outcomes if item.stage == "score@bench=a,model=small").status == (
        "artifact-missing"
    )
    assert missing.exists()
    clean(MatrixPipeline, Config(), cli_out=tmp_path, target="score", yes=True)
    assert not missing.exists()
    assert (out / ".matrix" / "source" / "bench=a" / "source.txt").exists()


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


def test_matrix_batch_output_must_stay_in_cell_directory(tmp_path: Path) -> None:
    class Bad(Pipeline):
        Config = Config

        @matrix(BENCH)
        @batch_stage()
        async def write(self, ctx: Ctx, *, bench: str):
            async for _, _ in ctx.resume([bench], progress=False):
                path = ctx.out / f"shared-{bench}.txt"
                path.write_text(bench)
                yield path

    with pytest.raises(ValueError, match="inside the output root"):
        run(Bad, Config(), cli_out=tmp_path)


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


@pytest.mark.parametrize(
    ("only", "downstream", "slices", "expected_probes"),
    [
        (
            "score",
            None,
            (),
            ["source@bench=a", "source@bench=b"],
        ),
        (
            "finish",
            None,
            (),
            [
                "source@bench=a",
                "source@bench=b",
                "score@bench=a,model=small",
                "score@bench=a,model=large",
                "score@bench=b,model=small",
                "score@bench=b,model=large",
            ],
        ),
        (
            None,
            "score",
            (),
            ["source@bench=a", "source@bench=b"],
        ),
        (
            "score",
            None,
            ("model=small",),
            [],
        ),
        (
            None,
            "score",
            ("model=small",),
            [],
        ),
    ],
)
def test_matrix_scoped_runs_probe_exact_external_closure_in_graph_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    only: str | None,
    downstream: str | None,
    slices: tuple[str, ...],
    expected_probes: list[str],
) -> None:
    run(MatrixPipeline, Config(), cli_out=tmp_path)
    probed: list[str] = []
    original = runner_module._probe_stage

    def counted(*call_args, **call_kwargs):
        result = original(*call_args, **call_kwargs)
        probed.append(result.stage)
        return result

    monkeypatch.setattr(runner_module, "_probe_stage", counted)

    run(
        MatrixPipeline,
        Config(),
        cli_out=tmp_path,
        only=only,
        downstream=downstream,
        slices=slices,
    )

    assert probed == expected_probes


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
                yield Path(f"{index}.txt")

    run(Batched, Config(), cli_out=tmp_path)
    out = tmp_path / "main"
    assert (out / ".matrix" / "parts" / "bench=a" / "0.txt").read_text() == "a"
    assert (out / ".matrix" / "parts" / "bench=b" / "0.txt").read_text() == "b"
    record = Store(out).read_success("parts@bench=a")
    assert record is not None
    assert record.outputs is not None
    assert [item.path for item in record.outputs] == [".matrix/parts/bench=a/0.txt"]


def _without_matrix_layout(components):
    values = dict(components.values)
    assert values.pop("__varve_matrix_layout__") == 2
    return components.model_copy(update={"values": values})


def test_legacy_flat_single_record_is_stale_and_rebuilt_in_new_layout(tmp_path: Path) -> None:
    axis = Axis("item", ["one"])

    class LegacySingle(Pipeline):
        Config = Config

        @matrix(axis)
        @stage(produces="result.txt")
        def write(self, ctx: Ctx, *, item: str) -> None:
            ctx.cell_out.mkdir(parents=True, exist_ok=True)
            (ctx.cell_out / "result.txt").write_text(f"new-{item}")

    out = tmp_path / "main"
    graph = build_graph(LegacySingle)
    stage_name = "write@item=one"
    probe = probe_pipeline(
        LegacySingle,
        Config(),
        args=LegacySingle.Args(),
        out=out,
        graph=graph,
    )[0]
    assert probe.components is not None
    legacy_components = _without_matrix_layout(probe.components)
    legacy_path = out / stage_name / "result.txt"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text("legacy")
    Store(out).write_success(
        SuccessRecord(
            pipeline=LegacySingle.__name__,
            stage=stage_name,
            kind="single",
            content_key=content_key(legacy_components),
            key_components=legacy_components,
            produces=[ProducedPath(path=f"{stage_name}/result.txt", kind="file")],
            committed_at="legacy",
        )
    )

    outcomes = run(LegacySingle, Config(), cli_out=tmp_path, graph=graph)
    record = Store(out).read_success(stage_name)

    assert outcomes[0].status == "stale"
    assert legacy_path.read_text() == "legacy"
    assert (out / ".matrix" / "write" / "item=one" / "result.txt").read_text() == "new-one"
    assert record is not None
    assert record.produces == [ProducedPath(path=".matrix/write/item=one/result.txt", kind="file")]


def test_legacy_flat_batch_partial_does_not_resume_or_mix_outputs(tmp_path: Path) -> None:
    axis = Axis("item", ["one"])
    executed: list[int] = []

    class LegacyBatch(Pipeline):
        Config = Config

        @matrix(axis)
        @batch_stage()
        async def write(self, ctx: Ctx, *, item: str):
            async for index, value in ctx.resume([f"{item}-0", f"{item}-1"], progress=False):
                executed.append(index)
                ctx.cell_out.mkdir(parents=True, exist_ok=True)
                path = ctx.cell_out / f"{index}.txt"
                path.write_text(value)
                yield path

    out = tmp_path / "main"
    graph = build_graph(LegacyBatch)
    stage_name = "write@item=one"
    probe = probe_pipeline(
        LegacyBatch,
        Config(),
        args=LegacyBatch.Args(),
        out=out,
        graph=graph,
    )[0]
    assert probe.components is not None
    legacy_key = content_key(_without_matrix_layout(probe.components))
    legacy_path = out / stage_name / "0.txt"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text("legacy")
    Store(out).write_batch(
        stage_name,
        legacy_key,
        BatchRecord(index=0, yielded=[f"{stage_name}/0.txt"], committed_at="legacy"),
    )

    outcomes = run(LegacyBatch, Config(), cli_out=tmp_path, graph=graph)
    record = Store(out).read_success(stage_name)

    assert outcomes[0].status == "no-cache"
    assert executed == [0, 1]
    assert legacy_path.read_text() == "legacy"
    assert record is not None
    assert record.outputs is not None
    assert [item.path for item in record.outputs] == [
        ".matrix/write/item=one/0.txt",
        ".matrix/write/item=one/1.txt",
    ]
