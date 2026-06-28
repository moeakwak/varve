from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from pydantic import BaseModel
from rich.console import Console

from varve import Experiment, stage
from varve.dashboard import render
from varve.dashboard.cli import main
from varve.dashboard.models import ExperimentEntry, ExperimentState, StageState, StateError
from varve.dashboard.render import render_detail, render_overview
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


def test_ls_and_show_can_include_temporary_branches(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    Store(tmp_path / "demo" / "out" / "main").ensure_initialized(
        "Demo",
        module="varve.no_such_main",
    )
    Store(tmp_path / "demo" / "out" / ".tmp" / "quick").ensure_initialized(
        "Demo",
        module="varve.no_such_temp",
        temporary_config={},
    )

    assert main(["ls", "--root", str(tmp_path)]) == 0
    default_output = capsys.readouterr().out
    assert "quick" not in default_output

    assert main(["ls", "--root", str(tmp_path), "--include-temp"]) == 0
    include_temp_output = capsys.readouterr().out
    assert "demo" in include_temp_output
    assert "main" in include_temp_output
    assert "quick" in include_temp_output

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.chdir(tmp_path)
        assert main(["--include-temp"]) == 0
    finally:
        monkeypatch.undo()
    default_command_output = capsys.readouterr().out
    assert "quick" in default_command_output

    assert main(["show", "demo", "--branch", "quick", "--root", str(tmp_path)]) == 1
    assert "Unknown experiment: demo (branch quick)" in capsys.readouterr().err

    assert main(
        ["show", "demo", "--branch", "quick", "--root", str(tmp_path), "--include-temp"]
    ) == 0
    detail = capsys.readouterr().out
    assert "Status: error" in detail
    assert "Error: import:" in detail


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


def test_refresh_runs_only_stale_entries_in_discovery_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = [
        ExperimentEntry(
            output_root=tmp_path / "stale" / "out" / "main",
            experiment_id="stale",
            experiment_name="Stale",
            branch="main",
            module="tests.demo",
        ),
        ExperimentEntry(
            output_root=tmp_path / "hit" / "out" / "main",
            experiment_id="hit",
            experiment_name="Hit",
            branch="main",
            module="tests.demo",
        ),
    ]

    def fake_discover(root: Path, *, include_temporary: bool = False):
        assert root == tmp_path
        assert include_temporary is True
        return entries

    def fake_state(entry: ExperimentEntry):
        status = "stale" if entry.experiment_id == "stale" else "hit"
        return ExperimentState(entry=entry, stages=[], status=status, error=None)

    refreshed: list[tuple[str, str]] = []
    monkeypatch.setattr("varve.dashboard.cli.discover_experiments", fake_discover)
    monkeypatch.setattr("varve.dashboard.cli.load_state", fake_state)
    monkeypatch.setattr(
        "varve.dashboard.cli._run_entry",
        lambda entry: refreshed.append((entry.experiment_id, entry.branch)),
    )

    assert main(["refresh", "--root", str(tmp_path), "--include-temp"]) == 0

    captured = capsys.readouterr()
    assert refreshed == [("stale", "main")]
    assert "Refreshing stale --branch main" in captured.out


def test_refresh_prefix_filters_entries_by_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [
        ExperimentEntry(
            output_root=tmp_path / "match" / "out" / "main",
            experiment_id="match",
            experiment_name="Match",
            branch="main",
            module="studies.exp.analysis.match",
        ),
        ExperimentEntry(
            output_root=tmp_path / "other" / "out" / "main",
            experiment_id="other",
            experiment_name="Other",
            branch="main",
            module="studies.exp.audit.other",
        ),
        ExperimentEntry(
            output_root=tmp_path / "legacy" / "out" / "main",
            experiment_id="legacy",
            experiment_name="Legacy",
            branch="main",
            module=None,
        ),
    ]

    monkeypatch.setattr(
        "varve.dashboard.cli.discover_experiments",
        lambda root, *, include_temporary=False: entries,
    )
    checked: list[str] = []

    def fake_state(entry: ExperimentEntry):
        checked.append(entry.experiment_id)
        return ExperimentState(entry=entry, stages=[], status="stale", error=None)

    refreshed: list[str] = []
    monkeypatch.setattr("varve.dashboard.cli.load_state", fake_state)
    monkeypatch.setattr(
        "varve.dashboard.cli._run_entry",
        lambda entry: refreshed.append(entry.experiment_id),
    )

    assert main(["refresh", "--root", str(tmp_path), "--prefix", "studies.exp.analysis"]) == 0

    assert checked == ["match"]
    assert refreshed == ["match"]


def test_refresh_noops_when_no_entries_are_stale(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entry = ExperimentEntry(
        output_root=tmp_path / "hit" / "out" / "main",
        experiment_id="hit",
        experiment_name="Hit",
        branch="main",
        module="tests.demo",
    )

    monkeypatch.setattr(
        "varve.dashboard.cli.discover_experiments",
        lambda root, *, include_temporary=False: [entry],
    )
    monkeypatch.setattr(
        "varve.dashboard.cli.load_state",
        lambda item: ExperimentState(entry=item, stages=[], status="hit", error=None),
    )
    monkeypatch.setattr(
        "varve.dashboard.cli._run_entry",
        lambda item: pytest.fail("refresh should skip non-stale entries"),
    )

    assert main(["refresh", "--root", str(tmp_path)]) == 0

    captured = capsys.readouterr()
    assert captured.out == "No stale experiments found\n"


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


def test_render_overview_groups_repeated_experiment_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = StringIO()

    def console_factory(**kwargs):
        return Console(
            file=buffer,
            force_terminal=False,
            width=120,
            **kwargs,
        )

    monkeypatch.setattr(render, "Console", console_factory)
    states = [
        ExperimentState(
            entry=ExperimentEntry(
                output_root=tmp_path / "demo" / "out" / "main",
                experiment_id="demo",
                experiment_name="Demo",
                branch="main",
            ),
            stages=[],
            status="hit",
            error=None,
        ),
        ExperimentState(
            entry=ExperimentEntry(
                output_root=tmp_path / "demo" / "out" / ".tmp" / "quick",
                experiment_id="demo",
                experiment_name="Demo",
                branch="quick",
            ),
            stages=[],
            status="hit",
            error=None,
        ),
    ]

    render_overview(states)

    output = buffer.getvalue()
    assert output.count("demo") == 1
    assert "main" in output
    assert "quick" in output


def test_render_overview_shows_total_stage_elapsed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = StringIO()

    def console_factory(**kwargs):
        return Console(
            file=buffer,
            force_terminal=False,
            width=120,
            **kwargs,
        )

    monkeypatch.setattr(render, "Console", console_factory)
    state = ExperimentState(
        entry=ExperimentEntry(
            output_root=tmp_path / "demo" / "out" / "main",
            experiment_id="demo",
            experiment_name="Demo",
            branch="main",
        ),
        stages=[
            StageState(
                name="sample",
                status="hit",
                reason="hit",
                artifacts=[],
                committed_at=None,
                upstreams=[],
                elapsed=1.25,
            ),
            StageState(
                name="summary",
                status="hit",
                reason="hit",
                artifacts=[],
                committed_at=None,
                upstreams=["sample"],
                elapsed=2.5,
            ),
        ],
        status="hit",
        error=None,
    )

    render_overview([state])

    output = buffer.getvalue()
    assert "DURATION" in output
    assert "3.75s" in output
