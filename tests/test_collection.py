from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from varve import KeySpec, Pipeline, batch_stage, stage


def test_decorators_capture_stage_metadata() -> None:
    @batch_stage(needs="sample", partition_key=["batch_size"])
    async def transform(ctx):  # pragma: no cover - metadata only
        yield ctx

    spec = transform.__varve_stage__
    assert spec.name == "transform"
    assert spec.kind == "batch"
    assert spec.needs == ("sample",)
    assert spec.keyspec == KeySpec()
    assert spec.partition_key == ("batch_size",)


class DemoConfig(BaseModel):
    profile: str = "default"


def test_experiment_collects_and_sorts_stages() -> None:
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


def test_experiment_rejects_unknown_dependencies() -> None:
    class Broken(Pipeline):
        Config = DemoConfig

        @stage(needs="missing")
        def downstream(self, ctx):  # pragma: no cover - metadata only
            return None

    with pytest.raises(ValueError, match="Unknown varve stage dependencies"):
        Broken.stages()


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
