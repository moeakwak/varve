from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from varve import Dependencies, Pipeline, batch_stage, stage
from varve.dependencies import merge_dependencies


def test_decorators_capture_stage_metadata() -> None:
    @batch_stage(needs="sample")
    async def transform(ctx):  # pragma: no cover - metadata only
        yield ctx

    spec = transform.__varve_stage__
    assert spec.name == "transform"
    assert spec.kind == "batch"
    assert spec.needs == ("sample",)
    assert spec.depends == Dependencies()


class DemoConfig(BaseModel):
    profile: str = "default"


def test_pipeline_collects_and_sorts_stages() -> None:
    class Demo(Pipeline):
        Config = DemoConfig

        @stage(produces="sample.txt")
        def sample(self, ctx):  # pragma: no cover - metadata only
            return None

        @stage(needs="sample", produces="summary.txt")
        def summarize(self, ctx):  # pragma: no cover - metadata only
            return None

    assert set(Demo.stages()) == {"sample", "summarize"}
    assert Demo.stages()["summarize"].kind == "single"
    assert Demo.topo_order() == ["sample", "summarize"]


def test_pipeline_rejects_unknown_dependencies() -> None:
    class Broken(Pipeline):
        Config = DemoConfig

        @stage(needs="missing")
        def downstream(self, ctx):  # pragma: no cover - metadata only
            return None

    with pytest.raises(ValueError, match="Unknown varve stage dependencies"):
        Broken.stages()


def test_pipeline_accepts_method_reference_dependencies() -> None:
    class Demo(Pipeline):
        Config = DemoConfig

        @stage(produces="sample.txt")
        def sample(self, ctx):  # pragma: no cover - metadata only
            return None

        @stage(needs=sample, produces="summary.txt")
        def summarize(self, ctx):  # pragma: no cover - metadata only
            return None

    assert Demo.stages()["summarize"].needs == ("sample",)
    assert Demo.topo_order() == ["sample", "summarize"]


def test_merge_dependencies_combines_review_sources_and_rejects_duplicate_names() -> None:
    base = Dependencies(
        inputs={"shared": lambda ctx: Path("base.txt")},
        values={"token": lambda ctx: "base"},
        sources=[Path("a.py"), Path("b.py")],
        review_sources=[Path("review_a.py"), Path("review_b.py")],
    )
    stage = Dependencies(
        inputs={"stage": lambda ctx: Path("stage.txt")},
        values={"limit": lambda ctx: 1},
        sources=[Path("b.py"), Path("c.py")],
        review_sources=[Path("review_b.py"), Path("review_c.py")],
    )
    merged = merge_dependencies(base, stage)
    assert list(merged.inputs) == ["shared", "stage"]
    assert list(merged.values) == ["token", "limit"]
    assert merged.sources == (Path("a.py"), Path("b.py"), Path("c.py"))
    assert merged.review_sources == (
        Path("review_a.py"),
        Path("review_b.py"),
        Path("review_c.py"),
    )
    with pytest.raises(ValueError, match="Duplicate pipeline and stage dependencies"):
        merge_dependencies(
            Dependencies(inputs={"shared": lambda ctx: Path("base.txt")}),
            Dependencies(inputs={"shared": lambda ctx: Path("stage.txt")}),
        )


def test_output_root_default_resolution(tmp_path: Path) -> None:
    class Demo(Pipeline):
        Config = DemoConfig

        @classmethod
        def default_output_root(cls, config: DemoConfig) -> Path:
            return tmp_path / "default-out"

        @stage()
        def sample(self, ctx):  # pragma: no cover - metadata only
            return None

    assert Demo.default_output_root(DemoConfig()) == tmp_path / "default-out"
    assert Demo.output_root(DemoConfig()) == tmp_path / "default-out" / "main"
    assert Demo.output_root(DemoConfig(), branch="exp1") == tmp_path / "default-out" / "exp1"
    assert Demo.output_root(DemoConfig(), branch="quick", is_temporary=True) == (
        tmp_path / "default-out" / ".tmp" / "quick"
    )
    assert Demo.output_root(DemoConfig(), cli_out=tmp_path / "cli-out") == (
        tmp_path / "cli-out" / "main"
    )
    with pytest.raises(ValueError, match="branch name"):
        Demo.output_root(DemoConfig(), branch="bad/name")
