from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from pydantic import BaseModel
from rich.console import Console

from varve import Experiment, stage
from varve.dashboard import render
from varve.dashboard.cli import main
from varve.dashboard.models import ExperimentEntry, ExperimentState, StateError
from varve.dashboard.render import render_detail
from varve.engine.runner import run
from varve.store.store import Store


class Config(BaseModel):
    pass


class Args(BaseModel):
    pass


class CliDemo(Experiment):
    Config = Config
    Args = Args

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text("sample", encoding="utf-8")

    @stage(needs="sample", produces="summary.txt")
    def summary(self, ctx):
        (ctx.out / "summary.txt").write_text("summary", encoding="utf-8")


def _run_demo(output_base: Path) -> None:
    run(CliDemo, Config(), args=Args(), cli_out=output_base)


def test_ls_and_show_render_engine_state(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_base = tmp_path / "alpha" / "out"
    output_root = output_base / "main"
    _run_demo(output_base)

    assert main(["ls", "--root", str(tmp_path)]) == 0
    ls = capsys.readouterr()
    assert "STATUS" in ls.out
    assert "alpha" in ls.out
    assert "hit" in ls.out
    assert "2/2" in ls.out

    assert main(["show", "alpha", "--root", str(tmp_path)]) == 0
    detail = capsys.readouterr()
    assert "Status: hit" in detail.out
    assert "REASON" in detail.out
    assert "sample" in detail.out
    assert str(output_root) in detail.out


def test_show_renders_error_diagnostics(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_root = tmp_path / "broken" / "out" / "main"
    Store(output_root).ensure_initialized("MissingExperiment", module="varve.no_such_module")

    assert main(["show", "broken", "--root", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "Status: error" in output
    assert "Error: import:" in output


def test_ls_returns_nonzero_for_empty_scan_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["ls", "--root", str(tmp_path / "missing")]) == 1

    captured = capsys.readouterr()
    assert "No experiments found" in captured.err
    assert captured.out == ""


def test_show_returns_nonzero_and_lists_known_ids_for_unknown_experiment(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    Store(tmp_path / "alpha" / "out" / "main").ensure_initialized(
        "Alpha",
        module="varve.no_such_alpha",
    )
    Store(tmp_path / "beta" / "out" / "main").ensure_initialized(
        "Beta",
        module="varve.no_such_beta",
    )

    assert main(["show", "missing", "--root", str(tmp_path)]) == 1

    captured = capsys.readouterr()
    assert "Unknown experiment: missing" in captured.err
    assert "alpha --branch main" in captured.err
    assert "beta --branch main" in captured.err


def test_no_subcommand_defaults_to_ls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_demo(tmp_path / "default" / "out")
    monkeypatch.chdir(tmp_path)

    assert main([]) == 0

    captured = capsys.readouterr()
    assert "default" in captured.out


def test_render_detail_styles_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = StringIO()

    def console_factory(**kwargs):
        return Console(
            file=buffer,
            force_terminal=True,
            color_system="standard",
            no_color=False,
            width=120,
            **kwargs,
        )

    monkeypatch.setattr(render, "Console", console_factory)
    state = ExperimentState(
        entry=ExperimentEntry(
            output_root=tmp_path,
            experiment_id="demo",
            experiment_name="Demo",
            branch="main",
        ),
        stages=[],
        status="error",
        error=StateError(phase="import", message="missing"),
    )

    render_detail(state)

    assert "Status: \x1b[31merror\x1b[0m" in buffer.getvalue()
    assert "Error: import: missing" in buffer.getvalue()
