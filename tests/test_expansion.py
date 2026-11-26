from __future__ import annotations

import time
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

from varve import Axis, Pipeline, stage
from varve.axis import AxisDomain
from varve.graph import (
    AlignmentError,
    GraphError,
    StageGraph,
    Unit,
    align_request,
    align_units,
    dynamic_requests,
    expand_stage,
    publish_targets,
    static_domains,
)


class Group(Enum):
    TEXFORM = "texform"
    CHEAP = "cheap"


DATASET = Axis("dataset", ("mw", "im2latex"))
GROUP = Axis("cost_group", (Group.TEXFORM, Group.CHEAP))
TARS = Axis(
    "tar",
    resolver=lambda ctx: {p.stem: p for p in sorted(Path(ctx.config["src"]).glob("*.tar"))},
    input_fingerprint="stat",
)
SHARDS = Axis("shard", resolver=lambda ctx, dataset: list(range(2 if dataset == "mw" else 3)))
BATCHES = Axis(
    "batch",
    resolver=lambda ctx, dataset, prepare: {int(p.stem): p for p in sorted(prepare.glob("*.json"))},
)


class Shapes(Pipeline):
    @stage()
    def single(self) -> None:
        pass

    @stage(foreach=(DATASET, GROUP), publish={"s.md": "{dataset}/{cost_group}.md"})
    def matrix(self, dataset: str, cost_group: Group) -> None:
        pass

    @stage(foreach=TARS, publish=["out.txt", "img/*.png"])
    def extract(self, tar: Path) -> None:
        pass

    @stage(foreach=(DATASET, SHARDS))
    def sharded(self, shard: int) -> None:
        pass

    @stage(foreach=DATASET)
    def prepare(self) -> None:
        pass

    @stage(foreach=(DATASET, GROUP, BATCHES))
    def safety(self, batch: Path) -> None:
        pass

    @stage()
    def finalize(self, safety: dict, matrix: dict, single: Path) -> None:
        pass

    @stage(foreach=DATASET)
    def per_dataset(self, matrix: dict, prepare: Path, sharded: dict) -> None:
        pass


def resolve(
    graph: StageGraph, name: str, statics: dict[Axis, AxisDomain], config: Any = None, **up: Any
) -> dict:
    """Test stand-in for the engine: call resolvers and normalize their results."""

    from varve.dependencies import DepCtx

    domains = {}
    for request in dynamic_requests(graph, graph.nodes[name], statics):
        binding = graph.resolvers[request.axis]
        kwargs: dict[str, Any] = {}
        for param in binding.params:
            if param.kind == "ctx":
                kwargs["ctx"] = DepCtx(config=config, cell=request.cell)
            elif param.kind == "axis":
                kwargs[param.name] = request.cell[param.name]
            else:
                upstream_node, upstream_units, to_path = up[param.name]
                kwargs[param.name] = to_path(align_request(request, upstream_node, upstream_units))
        assert request.axis.resolver is not None
        domains[request.key] = request.axis.domain_from_resolved(request.axis.resolver(**kwargs))
    return domains


def fan(binding: Unit | dict) -> dict:
    assert isinstance(binding, dict)
    return binding


@pytest.fixture
def graph() -> StageGraph:
    return Shapes.graph()


def test_zero_dim_stage_has_one_unit(graph: StageGraph) -> None:
    (unit,) = expand_stage(graph, graph.nodes["single"], static_domains(graph))
    assert unit.unit_key == "" and unit.relpath == "" and unit.coords == {}


def test_static_stage_expands_cartesian_product(graph: StageGraph) -> None:
    units = expand_stage(graph, graph.nodes["matrix"], static_domains(graph))
    assert [u.unit_key for u in units] == [
        "dataset=mw,cost_group=texform",
        "dataset=mw,cost_group=cheap",
        "dataset=im2latex,cost_group=texform",
        "dataset=im2latex,cost_group=cheap",
    ]
    assert units[0].values == ("mw", Group.TEXFORM)
    assert units[0].cell.cost_group is Group.TEXFORM
    assert units[0].relpath == "dataset=mw/cost_group=texform"
    assert units[0].axis_inputs() == {} and units[0].axis_values() == {}


def test_branch_active_axes_restrict_static_domains(graph: StageGraph) -> None:
    statics = static_domains(graph, {"dataset": ["im2latex"]})
    units = expand_stage(graph, graph.nodes["matrix"], statics)
    assert [u.ids for u in units] == [("im2latex", "texform"), ("im2latex", "cheap")]
    with pytest.raises(GraphError, match="Unknown static axis 'tar'"):
        static_domains(graph, {"tar": ["x"]})
    with pytest.raises(ValueError, match="Unknown id"):
        static_domains(graph, {"dataset": ["nope"]})


def test_dynamic_only_stage(graph: StageGraph, tmp_path: Path) -> None:
    for name in ("b", "a"):
        (tmp_path / f"{name}.tar").write_text(name)
    statics = static_domains(graph)
    node = graph.nodes["extract"]
    (request,) = dynamic_requests(graph, node, statics)
    assert request.ids == () and len(request.cell) == 0
    units = expand_stage(
        graph, node, statics, resolve(graph, "extract", statics, {"src": tmp_path})
    )
    assert [u.unit_key for u in units] == ["tar=a", "tar=b"]
    inputs = units[0].axis_inputs()["tar"]
    assert inputs.paths == (tmp_path / "a.tar",) and inputs.mode == "stat"
    with pytest.raises(KeyError, match="not resolved"):
        expand_stage(graph, node, statics, {})


def test_static_by_dynamic_stage_resolves_per_cell(graph: StageGraph) -> None:
    statics = static_domains(graph)
    node = graph.nodes["sharded"]
    requests = dynamic_requests(graph, node, statics)
    assert [(r.ids, dict(r.cell)) for r in requests] == [
        (("mw",), {"dataset": "mw"}),
        (("im2latex",), {"dataset": "im2latex"}),
    ]
    units = expand_stage(graph, node, statics, resolve(graph, "sharded", statics))
    assert [u.unit_key for u in units] == [
        "dataset=mw,shard=0",
        "dataset=mw,shard=1",
        "dataset=im2latex,shard=0",
        "dataset=im2latex,shard=1",
        "dataset=im2latex,shard=2",
    ]
    assert units[1].axis_values() == {"shard": 1}
    assert units[1].keys == ("mw", 1)


def test_upstream_driven_dynamic_stage(graph: StageGraph, tmp_path: Path) -> None:
    statics = static_domains(graph)
    prepare = graph.nodes["prepare"]
    prepare_units = expand_stage(graph, prepare, statics)
    for unit in prepare_units:
        out = tmp_path / unit.relpath
        out.mkdir(parents=True)
        for index in range(3 if unit.ids == ("mw",) else 1):
            (out / f"{index}.json").write_text("{}")

    def to_path(binding: Unit | dict) -> Path:
        assert isinstance(binding, Unit)  # dataset aligns 1-to-1 with the resolver cell
        return tmp_path / binding.relpath

    node = graph.nodes["safety"]
    assert [r.ids for r in dynamic_requests(graph, node, statics)] == [("mw",), ("im2latex",)]
    domains = resolve(graph, "safety", statics, prepare=(prepare, prepare_units, to_path))
    units = expand_stage(graph, node, statics, domains)
    assert len(units) == 2 * 3 + 2 * 1
    first = units[0]
    assert first.unit_key == "dataset=mw,cost_group=texform,batch=0"
    assert first.axis_inputs()["batch"].paths == (tmp_path / "dataset=mw" / "0.json",)
    assert first.axis_inputs()["batch"].mode == "content"


def test_alignment_fan_in_keys_and_broadcast(graph: StageGraph) -> None:
    statics = static_domains(graph)
    nodes = graph.nodes
    matrix = expand_stage(graph, nodes["matrix"], statics)
    prepare = expand_stage(graph, nodes["prepare"], statics)
    sharded = expand_stage(graph, nodes["sharded"], statics, resolve(graph, "sharded", statics))
    finalize = expand_stage(graph, nodes["finalize"], statics)
    per_dataset = expand_stage(graph, nodes["per_dataset"], statics)

    # Full fan-in over two axes: tuple keys in upstream foreach order, declared values kept.
    (whole,) = align_units(nodes["finalize"], finalize, nodes["matrix"], matrix)
    assert isinstance(whole, dict)
    assert list(whole) == [
        ("mw", Group.TEXFORM),
        ("mw", Group.CHEAP),
        ("im2latex", Group.TEXFORM),
        ("im2latex", Group.CHEAP),
    ]

    # Partial fan-in: the shared axis aligns, the remaining one becomes a scalar key.
    per = align_units(nodes["per_dataset"], per_dataset, nodes["matrix"], matrix)
    assert [list(fan(item)) for item in per] == [[Group.TEXFORM, Group.CHEAP]] * 2
    assert fan(per[0])[Group.CHEAP].unit_key == "dataset=mw,cost_group=cheap"
    shards = align_units(nodes["per_dataset"], per_dataset, nodes["sharded"], sharded)
    assert [list(fan(item)) for item in shards] == [[0, 1], [0, 1, 2]]  # dynamic int keys stay int

    # 1-to-1 on the shared axis.
    one = align_units(nodes["per_dataset"], per_dataset, nodes["prepare"], prepare)
    assert [u.unit_key for u in one] == ["dataset=mw", "dataset=im2latex"]  # type: ignore[union-attr]

    # Broadcast: downstream-only axes reuse the same upstream unit / shared fan-in dict.
    broadcast = align_units(nodes["matrix"], matrix, nodes["prepare"], prepare)
    assert broadcast[0] is broadcast[1] and broadcast[0] is not broadcast[2]
    single = expand_stage(graph, nodes["single"], statics)
    assert align_units(nodes["matrix"], matrix, nodes["single"], single) == single * 4


def test_alignment_reports_missing_upstream(graph: StageGraph) -> None:
    nodes = graph.nodes
    prepare = expand_stage(graph, nodes["prepare"], static_domains(graph, {"dataset": ["mw"]}))
    per_dataset = expand_stage(graph, nodes["per_dataset"], static_domains(graph))
    with pytest.raises(
        AlignmentError, match="No unit of 'prepare' matches {'dataset': 'im2latex'}"
    ):
        align_units(nodes["per_dataset"], per_dataset, nodes["prepare"], prepare)


def test_publish_targets_place_units_and_detect_collisions(
    graph: StageGraph, tmp_path: Path
) -> None:
    statics = static_domains(graph)
    matrix = expand_stage(graph, graph.nodes["matrix"], statics)
    assert [t.dst for t in publish_targets(graph.nodes["matrix"], matrix)][:2] == [
        "mw/texform.md",
        "mw/cheap.md",
    ]
    (tmp_path / "x.tar").write_text("x")
    extract = expand_stage(
        graph,
        graph.nodes["extract"],
        statics,
        resolve(graph, "extract", statics, {"src": tmp_path}),
    )
    assert [t.dst for t in publish_targets(graph.nodes["extract"], extract)] == [
        "tar=x/out.txt",
        "tar=x/img/*.png",
    ]

    left = Axis("a", ("x1", "x"))
    right = Axis("b", ("2", "12"))

    class Colliding(Pipeline):
        @stage(foreach=(left, right), publish={"r.txt": "{a}{b}.txt"})
        def s(self) -> None:
            pass

    colliding = Colliding.graph()
    units = expand_stage(colliding, colliding.nodes["s"], static_domains(colliding))
    with pytest.raises(GraphError, match="'x12.txt' is claimed by both"):
        publish_targets(colliding.nodes["s"], units)


def test_five_thousand_units_expand_and_align_in_linear_time() -> None:
    big = Axis("batch", resolver=lambda ctx, dataset: list(range(1250)))

    class Big(Pipeline):
        @stage(foreach=(DATASET, GROUP, big))
        def work(self) -> None:
            pass

        @stage(foreach=(DATASET, GROUP, big))
        def score(self, work: Path) -> None:
            pass

        @stage(foreach=DATASET)
        def merge(self, score: dict) -> None:
            pass

    graph = Big.graph()
    statics = static_domains(graph)
    domains = {
        (big, (dataset,)): big.domain_from_resolved(list(range(1250))) for dataset in DATASET.ids
    }
    nodes = graph.nodes
    best = float("inf")
    for _ in range(3):
        start = time.perf_counter()
        work = expand_stage(graph, nodes["work"], statics, domains)
        score = expand_stage(graph, nodes["score"], statics, domains)
        merge = expand_stage(graph, nodes["merge"], statics, domains)
        one_to_one = align_units(nodes["score"], score, nodes["work"], work)
        fan_in = align_units(nodes["merge"], merge, nodes["score"], score)
        best = min(best, time.perf_counter() - start)
    assert len(work) == len(score) == 5000
    assert [u.unit_key for u in one_to_one] == [u.unit_key for u in work]  # type: ignore[union-attr]
    assert [len(fan(item)) for item in fan_in] == [2500, 2500]
    assert best < 0.1, f"expansion + alignment of 5,000 units took {best * 1000:.1f} ms"
