from __future__ import annotations

import sys
from pathlib import Path

import pytest
from pydantic import BaseModel

from varve import Pipeline, stage


class Config(BaseModel):
    pass


class DefaultOutputPipeline(Pipeline):
    Config = Config

    @stage()
    def sample(self, ctx):  # pragma: no cover - metadata only
        return None


def test_default_output_root_uses_pipeline_module_out_dir() -> None:
    expected = Path(__file__).resolve().parent / "out"

    assert DefaultOutputPipeline.default_output_root(Config()) == expected
    assert DefaultOutputPipeline.output_root(Config(), branch="main") == expected / "main"
    assert DefaultOutputPipeline.output_root(
        Config(),
        branch="tmp",
        is_temporary=True,
    ) == (expected / ".tmp" / "tmp")


def test_default_output_root_falls_back_to_import_spec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SpecOnlyPipeline(DefaultOutputPipeline):
        pass

    SpecOnlyPipeline.__module__ = "varve.engine.state"
    monkeypatch.delitem(sys.modules, "varve.engine.state", raising=False)

    assert SpecOnlyPipeline.default_output_root(Config()) == (
        Path(__file__).resolve().parents[1] / "src" / "varve" / "engine" / "out"
    )


def test_import_module_name_uses_main_spec_for_module_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class MainPipeline(DefaultOutputPipeline):
        pass

    class Spec:
        name = "pkg.demo.__main__"

    module = type("Module", (), {"__spec__": Spec()})()
    MainPipeline.__module__ = "__main__"
    monkeypatch.setitem(sys.modules, "__main__", module)

    assert MainPipeline.import_module_name() == "pkg.demo.__main__"
