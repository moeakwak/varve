from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

from varve.branch import load_branches
from varve.dashboard.cli import main
from varve.dashboard.discovery import discover_pipelines, is_manual_entry
from varve.dashboard.models import PipelineEntry
from varve.dashboard.state import load_state


@pytest.fixture
def experiment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    package = tmp_path / "manual_demo"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "workflow.py").write_text(
        "from pydantic import BaseModel\n"
        "from varve import Pipeline, stage\n"
        "class Config(BaseModel):\n"
        "    token: str = 'default'\n"
        "class Demo(Pipeline):\n"
        "    Config = Config\n"
        "    @stage(produces='sample.txt')\n"
        "    def sample(self, ctx):\n"
        "        (ctx.out / 'sample.txt').write_text(ctx.config.token)\n"
    )
    (package / "varve.yaml").write_text(
        "main: {}\nmanual:\n  manual: true\n"
        "uninitialized:\n  manual: true\n"
        "temporary:\n  is_temporary: true\n  manual: true\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module("manual_demo.workflow")
    for branch in ("main", "manual", "temporary"):
        assert module.Demo.cli(["run", "--branch", branch]) == 0
    yield package, module.Demo
    sys.modules.pop("manual_demo.workflow", None)
    sys.modules.pop("manual_demo", None)


@pytest.mark.parametrize("value", ['"true"', "1", "null", "[]"])
def test_manual_requires_a_boolean(tmp_path: Path, value: str) -> None:
    config = tmp_path / "varve.yaml"
    config.write_text(f"main:\n  manual: {value}\n")
    with pytest.raises(ValueError, match="non-boolean manual"):
        load_branches(config)


def test_default_run_skips_manual_before_import_or_probe(
    experiment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package, _pipeline = experiment
    (package / "varve.yaml").write_text("main:\n  manual: true\nmanual:\n  manual: true\n")
    sys.modules.pop("manual_demo.workflow")
    sys.modules.pop("manual_demo")
    (package / "__init__.py").write_text("raise RuntimeError('must not import')\n")
    capsys.readouterr()
    assert main(["run", "--root", str(tmp_path)]) == 0
    output = capsys.readouterr().out
    assert "0 executed, 0 hit, 2 manual skipped" in output
    assert "manual_demo" not in sys.modules
    assert "manual_demo.workflow" not in sys.modules
    assert main(["run", "--root", str(tmp_path), "--all"]) == 1
    assert "must not import" in capsys.readouterr().out


def test_default_and_all_respect_scopes_and_only_existing_stores(
    experiment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package, _pipeline = experiment
    for path in package.glob("out/*/sample.txt"):
        path.unlink()
    capsys.readouterr()
    assert main(["run", "--root", str(tmp_path), "--prefix", "manual_demo"]) == 0
    assert "1 executed, 0 hit, 1 manual skipped" in capsys.readouterr().out
    assert not (package / "out/manual/sample.txt").exists()
    assert main(["run", "--all", "--root", str(tmp_path), "--branch", "main"]) == 0
    assert "0 executed, 1 hit, 0 manual skipped" in capsys.readouterr().out
    assert not (package / "out/manual/sample.txt").exists()
    assert main(["run", "--all", "--root", str(tmp_path)]) == 0
    assert "1 executed, 1 hit, 0 manual skipped" in capsys.readouterr().out
    assert not (package / "out/uninitialized").exists()
    assert main(["run", "--root", str(tmp_path), "--include-temp"]) == 0
    assert "0 executed, 1 hit, 2 manual skipped" in capsys.readouterr().out
    temporary_artifact = package / "out/.tmp/temporary/sample.txt"
    temporary_artifact.unlink()
    assert main(["run", "--all", "--root", str(tmp_path)]) == 0
    assert not temporary_artifact.exists()
    assert main(["run", "--all", "--root", str(tmp_path), "--include-temp"]) == 0
    assert temporary_artifact.exists()


def test_explicit_and_generated_run_ignore_manual_and_policy_does_not_change_keys(
    experiment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package, pipeline = experiment
    store = package / "out/main/.varve"
    before = {path.relative_to(store): path.read_bytes() for path in store.rglob("*.json")}
    (package / "varve.yaml").write_text("main:\n  manual: true\nmanual:\n  manual: true\n")
    entry = next(entry for entry in discover_pipelines(tmp_path) if entry.branch == "main")
    assert load_state(entry).status == "hit"
    assert main(["run", "manual_demo", "--root", str(tmp_path)]) == 0
    assert pipeline.cli(["run"]) == 0
    after = {path.relative_to(store): path.read_bytes() for path in store.rglob("*.json")}
    assert before == after
    (package / "out/manual/sample.txt").unlink()
    assert main(["run", "manual_demo", "--root", str(tmp_path), "--branch", "manual"]) == 0
    assert (package / "out/manual/sample.txt").exists()


def test_manual_uses_module_location_with_a_separate_output_root(
    experiment, tmp_path: Path
) -> None:
    _package, pipeline = experiment
    output = tmp_path / "elsewhere/out"
    assert pipeline.cli(["run", "--branch", "manual", "--out", str(output)]) == 0
    entry = discover_pipelines(output)[0]
    assert is_manual_entry(entry)


def test_manual_discovery_traverses_namespace_packages_without_importing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "namespace_studies/group/example"
    package.mkdir(parents=True)
    (package / "workflow.py").write_text("raise RuntimeError('must not import')\n")
    (package / "varve.yaml").write_text("main:\n  manual: true\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    entry = PipelineEntry(
        tmp_path / "elsewhere/out/main",
        "example",
        "Demo",
        "main",
        module="namespace_studies.group.example.workflow",
    )
    assert is_manual_entry(entry)
    assert "namespace_studies" not in sys.modules
    assert "namespace_studies.group.example.workflow" not in sys.modules


def test_invalid_policy_is_a_resolve_error_and_does_not_run(
    experiment, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    package, _pipeline = experiment
    (package / "out/main/sample.txt").unlink()
    (package / "varve.yaml").write_text("main:\n  manual: 'yes'\n")
    assert main(["run", "--root", str(tmp_path)]) == 1
    assert "non-boolean manual" in capsys.readouterr().out
    assert not (package / "out/main/sample.txt").exists()
