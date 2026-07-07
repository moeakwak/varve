from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import cast

import pytest
from pydantic import BaseModel
from rich.console import Console

from varve import Pipeline, stage
from varve.dashboard import render
from varve.dashboard.cli import main
from varve.dashboard.models import (
    PipelineEntry,
    PipelineState,
    PipelineStatus,
    StageState,
    StateError,
)
from varve.dashboard.render import render_detail, render_overview
from varve.engine.runner import run
from varve.store.store import Store


class Config(BaseModel):
    pass


class Args(BaseModel):
    pass


class CliDemo(Pipeline):
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
    Store(output_root).ensure_initialized("MissingPipeline", module="varve.no_such_module")

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

    assert main(["show", "demo", "--branch", "quick", "--root", str(tmp_path)]) == 1
    assert "Unknown pipeline: demo (branch quick)" in capsys.readouterr().err

    assert (
        main(["show", "demo", "--branch", "quick", "--root", str(tmp_path), "--include-temp"]) == 0
    )
    detail = capsys.readouterr().out
    assert "Status: error" in detail
    assert "Error: import:" in detail


def test_ls_returns_nonzero_for_empty_scan_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["ls", "--root", str(tmp_path / "missing")]) == 1

    captured = capsys.readouterr()
    assert "No pipelines found" in captured.err
    assert captured.out == ""


def test_show_returns_nonzero_and_lists_known_ids_for_unknown_pipeline(
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
    assert "Unknown pipeline: missing" in captured.err
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


def test_refresh_runs_executable_entries_in_discovery_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = [
        PipelineEntry(
            output_root=tmp_path / "stale" / "out" / "main",
            pipeline_id="stale",
            pipeline_name="Stale",
            branch="main",
            module="tests.demo",
        ),
        PipelineEntry(
            output_root=tmp_path / "dirty" / "out" / "main",
            pipeline_id="dirty",
            pipeline_name="Dirty",
            branch="main",
            module="tests.demo",
        ),
        PipelineEntry(
            output_root=tmp_path / "no-cache" / "out" / "main",
            pipeline_id="no-cache",
            pipeline_name="NoCache",
            branch="main",
            module="tests.demo",
        ),
        PipelineEntry(
            output_root=tmp_path / "resume" / "out" / "main",
            pipeline_id="resume",
            pipeline_name="Resume",
            branch="main",
            module="tests.demo",
        ),
        PipelineEntry(
            output_root=tmp_path / "artifact-missing" / "out" / "main",
            pipeline_id="artifact-missing",
            pipeline_name="ArtifactMissing",
            branch="main",
            module="tests.demo",
        ),
        PipelineEntry(
            output_root=tmp_path / "hit" / "out" / "main",
            pipeline_id="hit",
            pipeline_name="Hit",
            branch="main",
            module="tests.demo",
        ),
    ]

    def fake_discover(root: Path, *, include_temporary: bool = False):
        assert root == tmp_path
        assert include_temporary is True
        return entries

    def fake_state(entry: PipelineEntry):
        status = cast(PipelineStatus, entry.pipeline_id if entry.pipeline_id != "hit" else "hit")
        return PipelineState(entry=entry, stages=[], status=status, error=None)

    refreshed: list[tuple[str, str]] = []
    monkeypatch.setattr("varve.dashboard.cli.discover_pipelines", fake_discover)
    monkeypatch.setattr("varve.dashboard.cli.load_state", fake_state)
    monkeypatch.setattr(
        "varve.dashboard.cli._run_entry",
        lambda entry: refreshed.append((entry.pipeline_id, entry.branch)),
    )

    assert main(["refresh", "--root", str(tmp_path), "--include-temp"]) == 0

    captured = capsys.readouterr()
    assert refreshed == [
        ("stale", "main"),
        ("dirty", "main"),
        ("no-cache", "main"),
        ("resume", "main"),
        ("artifact-missing", "main"),
    ]
    assert "Refreshing stale --branch main" in captured.out
    assert "Refreshing dirty --branch main" in captured.out


def test_refresh_prefix_filters_entries_by_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = [
        PipelineEntry(
            output_root=tmp_path / "match" / "out" / "main",
            pipeline_id="match",
            pipeline_name="Match",
            branch="main",
            module="studies.exp.analysis.match",
        ),
        PipelineEntry(
            output_root=tmp_path / "other" / "out" / "main",
            pipeline_id="other",
            pipeline_name="Other",
            branch="main",
            module="studies.exp.audit.other",
        ),
        PipelineEntry(
            output_root=tmp_path / "legacy" / "out" / "main",
            pipeline_id="legacy",
            pipeline_name="Legacy",
            branch="main",
            module=None,
        ),
    ]

    monkeypatch.setattr(
        "varve.dashboard.cli.discover_pipelines",
        lambda root, *, include_temporary=False: entries,
    )
    checked: list[str] = []

    def fake_state(entry: PipelineEntry):
        checked.append(entry.pipeline_id)
        return PipelineState(entry=entry, stages=[], status="stale", error=None)

    refreshed: list[str] = []
    monkeypatch.setattr("varve.dashboard.cli.load_state", fake_state)
    monkeypatch.setattr(
        "varve.dashboard.cli._run_entry",
        lambda entry: refreshed.append(entry.pipeline_id),
    )

    assert main(["refresh", "--root", str(tmp_path), "--prefix", "studies.exp.analysis"]) == 0

    assert checked == ["match"]
    assert refreshed == ["match"]


def test_refresh_initializes_cli_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = PipelineEntry(
        output_root=tmp_path / "stale" / "out" / "main",
        pipeline_id="stale",
        pipeline_name="Stale",
        branch="main",
        module="tests.demo",
    )
    monkeypatch.setattr(
        "varve.dashboard.cli.discover_pipelines",
        lambda root, *, include_temporary=False: [entry],
    )
    monkeypatch.setattr(
        "varve.dashboard.cli.load_state",
        lambda item: PipelineState(entry=item, stages=[], status="stale", error=None),
    )
    calls: list[bool] = []
    monkeypatch.setattr(
        "varve.dashboard.cli.configure_cli_logging",
        lambda verbose=False: calls.append(verbose),
        raising=False,
    )
    monkeypatch.setattr("varve.dashboard.cli._run_entry", lambda item: None)

    assert main(["refresh", "--root", str(tmp_path)]) == 0

    assert calls == [False]


def test_refresh_noops_when_no_entries_are_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entry = PipelineEntry(
        output_root=tmp_path / "hit" / "out" / "main",
        pipeline_id="hit",
        pipeline_name="Hit",
        branch="main",
        module="tests.demo",
    )

    monkeypatch.setattr(
        "varve.dashboard.cli.discover_pipelines",
        lambda root, *, include_temporary=False: [entry],
    )
    monkeypatch.setattr(
        "varve.dashboard.cli.load_state",
        lambda item: PipelineState(entry=item, stages=[], status="hit", error=None),
    )
    monkeypatch.setattr(
        "varve.dashboard.cli._run_entry",
        lambda item: pytest.fail("refresh should skip non-executable entries"),
    )

    assert main(["refresh", "--root", str(tmp_path)]) == 0

    captured = capsys.readouterr()
    assert captured.out == "No executable pipelines found\n"


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

    monkeypatch.setattr(render, "make_console", console_factory)
    state = PipelineState(
        entry=PipelineEntry(
            output_root=tmp_path,
            pipeline_id="demo",
            pipeline_name="Demo",
            branch="main",
        ),
        stages=[],
        status="error",
        error=StateError(phase="import", message="missing"),
    )

    render_detail(state)

    assert "Status: \x1b[31merror\x1b[0m" in buffer.getvalue()
    assert "Error: import: missing" in buffer.getvalue()


def test_render_overview_groups_repeated_pipeline_names(
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

    monkeypatch.setattr(render, "make_console", console_factory)
    states = [
        PipelineState(
            entry=PipelineEntry(
                output_root=tmp_path / "demo" / "out" / "main",
                pipeline_id="demo",
                pipeline_name="Demo",
                branch="main",
            ),
            stages=[],
            status="hit",
            error=None,
        ),
        PipelineState(
            entry=PipelineEntry(
                output_root=tmp_path / "demo" / "out" / ".tmp" / "quick",
                pipeline_id="demo",
                pipeline_name="Demo",
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

    monkeypatch.setattr(render, "make_console", console_factory)
    state = PipelineState(
        entry=PipelineEntry(
            output_root=tmp_path / "demo" / "out" / "main",
            pipeline_id="demo",
            pipeline_name="Demo",
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
