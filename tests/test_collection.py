from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from varve import Experiment, KeySpec, batch_stage, stage


def test_keyspec_coerce() -> None:
    original = KeySpec(config=("x",))
    assert KeySpec.coerce(["a", "b"]).config == ("a", "b")
    assert KeySpec.coerce(original) is original
    assert KeySpec.coerce(None) == KeySpec()


def test_decorators_capture_stage_metadata() -> None:
    @batch_stage(needs="sample", key=["profile"], partition_key=["batch_size"])
    async def transform(ctx):  # pragma: no cover - metadata only
        yield ctx

    spec = transform.__varve_stage__
    assert spec.name == "transform"
    assert spec.kind == "batch"
    assert spec.needs == ("sample",)
    assert spec.keyspec.config == ("profile",)
    assert spec.partition_key == ("batch_size",)


class DemoConfig(BaseModel):
    out: Path


def test_experiment_collects_and_sorts_stages() -> None:
    class Demo(Experiment):
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
    class Broken(Experiment):
        Config = DemoConfig

        @stage(needs="missing")
        def downstream(self, ctx):  # pragma: no cover - metadata only
            return None

    with pytest.raises(ValueError, match="Unknown varve stage dependencies"):
        Broken.stages()


def test_output_root_default_resolution(tmp_path: Path) -> None:
    class Demo(Experiment):
        Config = DemoConfig

        @stage()
        def sample(self, ctx):  # pragma: no cover - metadata only
            return None

    assert Demo.output_root(DemoConfig(out=tmp_path)) == tmp_path

    class OutputRootConfig(BaseModel):
        output_root: Path

    assert Demo.output_root(OutputRootConfig(output_root=tmp_path)) == tmp_path

    with pytest.raises(ValueError, match="output_root"):
        Demo.output_root(object())

