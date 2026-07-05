from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def _load_module(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_cli_discovers_adjacent_varve_yaml(tmp_path: Path) -> None:
    module_path = tmp_path / "demo_exp.py"
    module_path.write_text(
        """
from pathlib import Path

from pydantic import BaseModel

from varve import Pipeline, stage


class Config(BaseModel):
    token: str = "default"


class DemoPipeline(Pipeline):
    Config = Config

    @classmethod
    def default_output_root(cls, config: Config) -> Path:
        return Path(__file__).resolve().parent / "out"

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(ctx.config.token, encoding="utf-8")
""",
        encoding="utf-8",
    )
    (tmp_path / "varve.yaml").write_text(
        "alt:\n  token: branch\n",
        encoding="utf-8",
    )
    module = _load_module(module_path, "demo_exp")

    assert module.DemoPipeline.cli(["run", "--branch", "alt"]) == 0

    assert (tmp_path / "out" / "alt" / "sample.txt").read_text(encoding="utf-8") == "branch"
