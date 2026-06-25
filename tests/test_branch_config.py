from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

from varve.branch import derive_override_branch, load_branch


def _load_module(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_load_branch_reads_named_section_and_temporary_flag(tmp_path: Path) -> None:
    branches = tmp_path / "branches.yaml"
    branches.write_text(
        "\n".join(
            [
                "main:",
                "  limit: 10",
                "smoke:",
                "  is_temporary: true",
                "  limit: 2",
            ]
        ),
        encoding="utf-8",
    )

    assert load_branch(branches, "main") == ({"limit": 10}, False)
    assert load_branch(branches, "smoke") == ({"limit": 2}, True)


def test_load_branch_defaults_missing_main_to_empty_config(tmp_path: Path) -> None:
    assert load_branch(None, "main") == ({}, False)
    assert load_branch(tmp_path / "missing.yaml", "main") == ({}, False)


def test_load_branch_rejects_missing_non_main_branch(tmp_path: Path) -> None:
    branches = tmp_path / "branches.yaml"
    branches.write_text("main:\n  limit: 10\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Unknown varve branch"):
        load_branch(branches, "smoke")


def test_load_branch_rejects_non_boolean_temporary_flag(tmp_path: Path) -> None:
    branches = tmp_path / "branches.yaml"
    branches.write_text("main:\n  is_temporary: 'false'\n", encoding="utf-8")

    with pytest.raises(ValueError, match="non-boolean is_temporary"):
        load_branch(branches, "main")


def test_derive_override_branch_deep_merges_and_names_deterministically() -> None:
    base = {"limit": 10, "nested": {"left": "keep", "right": "old"}}

    first = derive_override_branch(base, '{"nested":{"right":"new"}}', base_name="main")
    second = derive_override_branch(base, '{"nested":{"right":"new"}}', base_name="main")

    assert first == second
    merged, branch, is_temporary = first
    assert merged == {"limit": 10, "nested": {"left": "keep", "right": "new"}}
    assert branch.startswith("main_override_")
    assert is_temporary is True
    assert base["nested"]["right"] == "old"


def test_derive_override_branch_accepts_explicit_name() -> None:
    merged, branch, is_temporary = derive_override_branch(
        {"limit": 10},
        '{"limit": 2}',
        base_name="main",
        name="quick",
    )

    assert merged == {"limit": 2}
    assert branch == "quick"
    assert is_temporary is True


def test_cli_discovers_adjacent_branches_yaml(tmp_path: Path) -> None:
    module_path = tmp_path / "demo_exp.py"
    module_path.write_text(
        '''
from pathlib import Path

from pydantic import BaseModel

from varve import Experiment, stage


class Config(BaseModel):
    token: str = "default"


class DemoExperiment(Experiment):
    Config = Config

    @classmethod
    def default_output_root(cls, config: Config) -> Path:
        return Path(__file__).resolve().parent / "out"

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(ctx.config.token, encoding="utf-8")
''',
        encoding="utf-8",
    )
    (tmp_path / "branches.yaml").write_text(
        "alt:\n  token: branch\n",
        encoding="utf-8",
    )
    module = _load_module(module_path, "demo_exp")

    assert module.DemoExperiment.cli(["run", "--branch", "alt"]) == 0

    assert (tmp_path / "out" / "alt" / "sample.txt").read_text(encoding="utf-8") == "branch"
