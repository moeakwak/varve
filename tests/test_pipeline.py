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


def test_selector_is_the_package_that_owns_the_output_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class PackagePipeline(DefaultOutputPipeline):
        pass

    monkeypatch.setattr(PackagePipeline, "__module__", "pkg.demo.run")
    assert PackagePipeline.selector() == "pkg.demo"

    monkeypatch.setattr(PackagePipeline, "__module__", "pkg.demo.__main__")
    assert PackagePipeline.selector() == "pkg.demo"


def test_selector_falls_back_to_the_script_stem_for_module_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ScriptPipeline(DefaultOutputPipeline):
        pass

    module = type("Module", (), {"__spec__": None, "__file__": "/work/demo.py"})()
    monkeypatch.setattr(ScriptPipeline, "__module__", "__main__")
    monkeypatch.setitem(sys.modules, "__main__", module)

    assert ScriptPipeline.selector() == "demo"


def test_declared_varve_name_overrides_the_derived_selector() -> None:
    class NamedPipeline(DefaultOutputPipeline):
        varve_name = "team.experiments.demo"

    assert NamedPipeline.selector() == "team.experiments.demo"


def test_declared_varve_name_must_be_a_dotted_identifier_path() -> None:
    class InvalidPipeline(DefaultOutputPipeline):
        varve_name = "team experiments"

    with pytest.raises(ValueError, match="dotted path of Python identifiers"):
        InvalidPipeline.selector()
