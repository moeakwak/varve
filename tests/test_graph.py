from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from varve import Axis, Ctx, Dependencies, Pipeline, Ref, Resource, stage
from varve.decorators import PublishEntry
from varve.dependencies import DepCtx
from varve.graph import GraphError, build_graph
from varve.resource import RunCtx

DATASET = Axis("dataset", ("mw", "im2latex"))
GROUP = Axis("cost_group", ("texform", "cheap"))
ENGINE = Resource("engine", depends=Dependencies(values={"engine": "1.0"}))
ORACLE = Resource("oracle")


def kinds(pipeline: type[Pipeline], stage_name: str) -> dict[str, str]:
    return {p.name: p.kind for p in pipeline.graph().nodes[stage_name].params}


def test_stage_decorator_normalizes_bare_strings_and_publish() -> None:
    @stage(produces="a.txt", publish=True, exclusive="cost_group=texform", needs="x", version=2)
    def one(self: Any) -> None:
        pass

    spec = one.__varve_stage__  # type: ignore[attr-defined]
    assert spec.produces == ("a.txt",) and spec.produces_file == "a.txt"
    assert spec.publish == (PublishEntry("a.txt", "a.txt", False),)
    assert spec.exclusive == ((("cost_group", "texform"),),)
    assert spec.needs == ("x",)
    assert spec.version == "2"

    @stage(produces=["a.txt"], publish={"a.txt": "{dataset}/a.txt"}, foreach=DATASET)
    def two(self: Any) -> None:
        pass

    spec = two.__varve_stage__  # type: ignore[attr-defined]
    assert spec.produces_file is None
    assert spec.publish[0].templated and spec.publish[0].placeholders() == {"dataset"}


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"publish": True}, "requires produces"),
        ({"produces": "../x"}, "relative path"),
        ({"produces": "/abs"}, "relative path"),
        ({"publish": {"*.png": "x"}}, "needs a directory target"),
        ({"workers": 0}, "positive"),
        ({"max_failures": 0}, "positive"),
        ({"workers": True}, "positive"),
        ({"max_failures": True}, "positive"),
        ({"foreach": (DATASET, DATASET)}, "repeat"),
        ({"exclusive": "cost_group"}, "selector"),
        ({"backend": "async"}, "async def"),
    ],
)
def test_stage_decorator_rejects_bad_declarations(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises((ValueError, TypeError), match=message):

        @stage(**kwargs)
        def bad(self: Any) -> None:
            pass


def test_async_stage_rejects_process_backend() -> None:
    with pytest.raises(ValueError, match="plain def"):

        @stage(backend="process")
        async def bad(self: Any) -> None:
            pass


class Upstream(Pipeline):
    pass


class Bound(Pipeline):
    labels = Ref("dataset_audit.labels", stage="export", branch=["main", "katex"])

    @stage(foreach=DATASET)
    def prepare(self, ctx: Ctx, dataset: str) -> None:
        pass

    @stage(foreach=(DATASET, GROUP), resources=[ENGINE, ORACLE], needs="audit")
    def work(
        self, ctx: Ctx, dataset: str, cost_group: str, prepare: Path, labels: dict, engine: Any
    ) -> None:
        pass

    @stage()
    def audit(self) -> None:
        pass

    @stage(resources=lambda ctx: [ENGINE])
    def report(self, work: dict, oracle: Any) -> None:
        pass


def test_signature_binding_classifies_every_parameter() -> None:
    assert kinds(Bound, "work") == {
        "ctx": "ctx",
        "dataset": "axis",
        "cost_group": "axis",
        "prepare": "upstream",
        "labels": "ref",
        "engine": "resource",
    }
    # With a resources callable, unknown names are deferred to per-cell resolution.
    assert kinds(Bound, "report") == {"work": "upstream", "oracle": "resource"}
    node = Bound.graph().nodes["work"]
    assert node.upstream == ("prepare", "audit")
    assert node.refs == ("labels",)
    assert Bound.graph().order == ("prepare", "audit", "work", "report")


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("def s(self, missing): pass", r"cannot inject parameter 'missing'.*'ctx'"),
        ("def s(self, cost_group): pass", "not in foreach"),
        ("def s(self, *args): pass", r"\*args"),
        ("def s(self, s): pass", "cannot depend on itself"),
    ],
)
def test_signature_binding_errors(body: str, message: str) -> None:
    namespace: dict[str, Any] = {}
    exec(body, namespace)
    decorated = stage(foreach=DATASET)(namespace["s"])

    @stage(foreach=GROUP)
    def other(self: Any) -> None:
        pass

    broken = type("Broken", (Pipeline,), {"s": decorated, "other": other})
    with pytest.raises(GraphError, match=message):
        build_graph(broken)


def test_unknown_needs_and_cycles_are_rejected() -> None:
    class UnknownNeed(Pipeline):
        @stage(needs="ghost")
        def a(self) -> None:
            pass

    with pytest.raises(GraphError, match="unknown stage 'ghost'"):
        UnknownNeed.graph()

    class Cycle(Pipeline):
        @stage()
        def a(self, c: Path) -> None:
            pass

        @stage()
        def b(self, a: Path) -> None:
            pass

        @stage(needs="b")
        def c(self) -> None:
            pass

    with pytest.raises(GraphError, match=r"cycle: a -> c -> b -> a \(-> = depends on\)"):
        Cycle.graph()


def test_name_collisions_are_rejected() -> None:
    other = Axis("dataset", ("x",))

    class TwoAxes(Pipeline):
        @stage(foreach=DATASET)
        def a(self) -> None:
            pass

        @stage(foreach=other)
        def b(self) -> None:
            pass

    with pytest.raises(GraphError, match="unique within a pipeline"):
        TwoAxes.graph()

    class Clash(Pipeline):
        dataset = Ref("x", stage="y")

        @stage(foreach=DATASET)
        def a(self) -> None:
            pass

    with pytest.raises(GraphError, match="axis and a Ref"):
        Clash.graph()

    with pytest.raises(ValueError, match="shadows a Pipeline attribute"):

        class Shadow(Pipeline):
            @stage()
            def graph(self) -> None:  # type: ignore[override]
                pass

        Shadow.stages()


def test_resolver_binding_and_ordering_edges() -> None:
    batches = Axis("batch", resolver=lambda ctx, dataset, prepare: {})

    class Dyn(Pipeline):
        @stage(foreach=(DATASET, batches))
        def work(self, batch: Path) -> None:
            pass

        @stage(foreach=DATASET)
        def prepare(self) -> None:
            pass

    graph = Dyn.graph()
    binding = graph.resolvers[batches]
    assert binding.static_axes == (DATASET,) and binding.stages == ("prepare",)
    node = graph.nodes["work"]
    assert node.upstream == () and node.deps == ("prepare",)  # ordering only, not keyed
    assert graph.order == ("prepare", "work")
    assert graph.ancestors("work") == ["prepare"]
    assert graph.descendants("prepare") == ["work"]

    class Misordered(Pipeline):
        @stage(foreach=(batches, DATASET))
        def work(self) -> None:
            pass

        @stage(foreach=DATASET)
        def prepare(self) -> None:
            pass

    with pytest.raises(GraphError, match="must precede it"):
        Misordered.graph()

    bad = Axis("shard", resolver=lambda ctx, nope: [])

    class BadResolver(Pipeline):
        @stage(foreach=bad)
        def work(self) -> None:
            pass

    with pytest.raises(GraphError, match="resolver: cannot inject parameter 'nope'"):
        BadResolver.graph()


def test_publish_templates_must_cover_all_axes() -> None:
    class Partial(Pipeline):
        @stage(foreach=(DATASET, GROUP), publish={"s.md": "{dataset}/s.md"})
        def a(self) -> None:
            pass

    with pytest.raises(GraphError, match="every axis placeholder"):
        Partial.graph()

    class Unknown(Pipeline):
        @stage(publish={"s.md": "{dataset}/s.md"})
        def a(self) -> None:
            pass

    with pytest.raises(GraphError, match="unknown axes"):
        Unknown.graph()

    class Duplicate(Pipeline):
        @stage(publish={"a.md": "out.md", "b.md": "out.md"})
        def a(self) -> None:
            pass

    with pytest.raises(GraphError, match="two publish entries"):
        Duplicate.graph()

    # Directory targets may be shared by units and stages; files are checked when published.
    class Merged(Pipeline):
        @stage(foreach=(DATASET, GROUP), publish={"parts/*.csv": "parts/{dataset}/"})
        def a(self) -> None:
            pass

        @stage(publish={"parts/*.csv": "parts/a/"})
        def b(self) -> None:
            pass

    assert Merged.graph().nodes["a"].spec.publish[0].into_dir


def test_exclusive_selector_validation() -> None:
    class BadAxis(Pipeline):
        @stage(foreach=DATASET, exclusive="cost_group=texform")
        def a(self) -> None:
            pass

    with pytest.raises(GraphError, match="not in foreach"):
        BadAxis.graph()

    class BadValue(Pipeline):
        @stage(foreach=GROUP, exclusive="cost_group=gpu")
        def a(self) -> None:
            pass

    with pytest.raises(GraphError, match="not a value"):
        BadValue.graph()


def test_defaults_storage_validation_and_merging() -> None:
    shared = Resource("shared", depends=Dependencies(values={"v": 1}))

    class WithDefaults(Pipeline):
        defaults = dict(storage="interim", resources=[shared], workers=4, max_failures=3)

        @stage(storage="output", resources=[ENGINE], workers=lambda ctx: ctx.args.n)
        def a(self, shared: Any, engine: Any) -> None:
            pass

        @stage()
        def b(self) -> None:
            pass

    graph = WithDefaults.graph()
    a, b = graph.nodes["a"], graph.nodes["b"]
    assert (a.storage, b.storage) == ("output", "interim")
    assert b.workers == 4 and b.max_failures == 3 and b.backend == "auto"
    run_ctx = RunCtx(
        config=None, args=type("A", (), {"n": 7})(), branch="main", stage="a", root=Path()
    )
    assert a.resolve_workers(run_ctx) == 7
    resources = a.resources_for(DepCtx(config=None))
    assert [r.name for r in resources] == ["shared", "engine"]
    deps = a.dependencies_for(DepCtx(config=None), resources)
    assert dict(deps.values) == {"v": 1, "engine": "1.0"}
    assert graph.storage_names() == {"output", "interim"}
    graph.validate_storages({"default", "interim", "output"})
    with pytest.raises(GraphError, match="Unknown storage"):
        graph.validate_storages({"default", "interim"})

    class BadDefaults(Pipeline):
        defaults = dict(produces="x")

        @stage()
        def a(self) -> None:
            pass

    with pytest.raises(GraphError, match="unsupported keys"):
        BadDefaults.graph()


def test_duplicate_dependency_names_are_rejected() -> None:
    clash = Resource("clash", depends=Dependencies(values={"engine": "2"}))

    class Clashing(Pipeline):
        @stage(resources=[ENGINE, clash])
        def a(self) -> None:
            pass

    node = Clashing.graph().nodes["a"]
    with pytest.raises(ValueError, match="declared by both"):
        node.dependencies_for(DepCtx(config=None))


def test_needs_accepts_methods_and_unbound_refs() -> None:
    external = Ref("other.pipe", stage="final")

    class Needs(Pipeline):
        @stage()
        def a(self) -> None:
            pass

        @stage(needs=[a, external])
        def b(self) -> None:
            pass

    node = Needs.graph().nodes["b"]
    assert node.upstream == ("a",)
    assert node.refs == ("ref:other.pipe:final",)
    assert Needs.graph().refs["ref:other.pipe:final"] is external


def test_stage_inheritance_keeps_definition_order() -> None:
    class Base(Pipeline):
        @stage()
        def first(self) -> None:
            pass

        @stage()
        def second(self) -> None:
            pass

    class Child(Base):
        @stage()
        def first(self) -> None:  # override keeps position
            pass

        @stage()
        def third(self) -> None:
            pass

    assert list(Child.stages()) == ["first", "second", "third"]
    assert Child.stages()["first"].func is Child.__dict__["first"]


def test_resolver_refs_order_but_do_not_key_and_resource_clashes() -> None:
    shards = Axis("shard", resolver=lambda ctx, manifest: [])

    class RefResolver(Pipeline):
        manifest = Ref("up.pipe", stage="manifest")

        @stage(foreach=shards)
        def work(self) -> None:
            pass

    node = RefResolver.graph().nodes["work"]
    assert node.refs == () and node.resolver_refs == ("manifest",)

    clash = Resource("prepare")

    class Clash(Pipeline):
        @stage()
        def prepare(self) -> None:
            pass

        @stage(resources=[clash])
        def work(self) -> None:
            pass

    with pytest.raises(GraphError, match="resource names clash"):
        Clash.graph()


def test_literal_publish_targets_may_not_collide_across_stages() -> None:
    class Literal(Pipeline):
        @stage(produces="a.txt", publish=True)
        def one(self) -> None:
            pass

        @stage(publish={"b.txt": "a.txt"})
        def two(self) -> None:
            pass

    with pytest.raises(GraphError, match="'a.txt' is claimed by both stage 'one' and stage 'two'"):
        Literal.graph()

    class Distinct(Pipeline):
        @stage(produces="a.txt", publish=True)
        def one(self) -> None:
            pass

        @stage(foreach=DATASET, publish=["a.txt"])
        def two(self, dataset: str) -> None:  # placed under dataset=<id>/
            pass

    Distinct.graph()


def test_defaults_reject_bool_workers_and_max_failures() -> None:
    from varve.graph import GraphError

    for key in ("workers", "max_failures"):

        class Bad(Pipeline):
            defaults = {key: True}

            @stage()
            def only(self, ctx: Any) -> None:
                pass

        with pytest.raises(GraphError, match=f"defaults\\['{key}'\\] must be a positive int"):
            Bad.graph()
