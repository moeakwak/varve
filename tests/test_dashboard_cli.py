from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from pydantic import BaseModel
from rich.console import Console

from varve import Axis, Pipeline, matrix, stage
from varve.dashboard import commands
from varve.dashboard.cli import main
from varve.dashboard.models import PipelineEntry, PipelineState, StateError
from varve.dashboard.render import render_overview
from varve.engine.review import SourceReviewResult
from varve.engine.runner import run
from varve.engine.state import EffectiveStatus, ExecutionStatus
from varve.status import PipelineStatus, StageStatus


class Config(BaseModel):
    pass


class Args(BaseModel):
    workers: int = 1
    label: str = ""


class CliDemo(Pipeline):
    Config = Config
    Args = Args

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(str(ctx.args.workers), encoding="utf-8")

    @stage(needs="sample", produces="summary.txt")
    def summary(self, ctx):
        (ctx.out / "summary.txt").write_text("summary", encoding="utf-8")


class RequiredArgs(BaseModel):
    workers: int


class RequiredArgsDemo(Pipeline):
    Config = Config
    Args = RequiredArgs

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(str(ctx.args.workers), encoding="utf-8")


BENCH = Axis("bench", ["a", "b"])
MODEL = Axis("model", ["x", "y"])


class MatrixCliDemo(Pipeline):
    Config = Config
    Args = Args

    @matrix(BENCH, MODEL)
    @stage(produces="score.txt")
    def score(self, ctx, *, bench: str, model: str):
        ctx.cell_out.mkdir(parents=True, exist_ok=True)
        (ctx.cell_out / "score.txt").write_text(f"{bench}:{model}", encoding="utf-8")


def _run_demo(output_base: Path) -> PipelineEntry:
    run(CliDemo, Config(), args=Args(), cli_out=output_base)
    return PipelineEntry(
        output_root=output_base / "main",
        pipeline_id="demo",
        pipeline_name="CliDemo",
        branch="main",
        module=CliDemo.__module__,
    )


def _stage_status(
    status: EffectiveStatus,
    *,
    name: str = "sample",
    reason: str | None = None,
) -> StageStatus:
    execution: ExecutionStatus = "hit" if status == "needs-review" else status
    relationship = "changed" if status == "needs-review" else "current"
    return StageStatus(
        name=name,
        base_name=name,
        cell=(),
        needs=(),
        logical_needs=(),
        status=status,
        reason=reason or ("source-changed" if status == "needs-review" else status),
        summary_reason=reason or ("source-changed" if status == "needs-review" else status),
        execution_status=execution,
        execution_reason="hit" if status == "needs-review" else (reason or status),
        source_relationship=relationship,
        source_decision="none",
        duration=1.25,
        committed_at=None,
        decision_key=None,
        stored_key=None,
        key_inputs=None,
        source_changes={},
        unavailable_reason=None,
        failure="RuntimeError: failed" if status == "failed" else None,
    )


def _state(
    entry: PipelineEntry,
    status: EffectiveStatus,
    *,
    reason: str | None = None,
) -> PipelineState:
    if status == "error":
        return PipelineState(
            entry=entry,
            error=StateError(phase="import", message=reason or "broken"),
        )
    return PipelineState(
        entry=entry,
        pipeline_status=PipelineStatus(
            pipeline=entry.pipeline_name or "Demo",
            module=entry.module or "missing",
            branch=entry.branch,
            output_root=entry.output_root,
            stages=(_stage_status(status, reason=reason),),
        ),
    )


def test_top_level_help_lists_only_unified_commands(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--help"])
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    output = captured.out
    for command in ("ls", "status", "run", "reuse", "invalidate", "plan", "clean"):
        assert command in output
    assert "\n  show " not in output
    assert "\n  refresh " not in output

    for removed in ("show", "refresh"):
        with pytest.raises(SystemExit):
            main([removed])


@pytest.mark.parametrize("command", ["ls", "status", "run", "reuse", "invalidate", "plan", "clean"])
def test_top_level_subcommand_help_uses_unified_surface(command: str, capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main([command, "--help"])
    assert exc_info.value.code == 0
    output = capsys.readouterr().out
    if command in {"run", "reuse", "invalidate"}:
        assert "MODULE" in output and "--all" in output
    elif command in {"status", "plan", "clean"}:
        assert "MODULE" in output
    if command in {"status", "run", "plan", "clean"}:
        assert "STAGE_SELECTOR" in output
    if command in {"reuse", "invalidate"}:
        assert "--stage BASE_STAGE" in output


def test_top_level_has_no_bulk_clean() -> None:
    with pytest.raises(SystemExit):
        main(["clean", "--all"])


def test_bare_varve_is_exact_overview_and_uses_manifest_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_demo(tmp_path / "demo" / "out")
    monkeypatch.chdir(tmp_path)
    assert main([]) == 0
    output = capsys.readouterr().out
    assert CliDemo.__module__ in output
    assert "main" in output
    assert "hit" in output
    assert "REVIEW" not in output
    assert "STAGES" not in output


def test_ls_module_and_status_share_generated_renderers(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_demo(tmp_path / "demo" / "out")
    root = str(tmp_path)

    assert main(["ls", CliDemo.__module__, "--root", root]) == 0
    structure = capsys.readouterr().out
    assert all(column in structure for column in ("STAGE", "KIND", "NEEDS", "MATRIX"))
    assert "sample" in structure and "summary" in structure

    assert main(["status", CliDemo.__module__, "--root", root]) == 0
    status = capsys.readouterr().out
    assert CliDemo.__module__ in status
    assert "CliDemo" in status
    assert "sample" in status and "summary" in status
    assert "REVIEW" in status

    assert main(["run", CliDemo.__module__, "--root", root]) == 0
    run_output = capsys.readouterr().out
    assert "STAGE" in run_output and "STATUS" in run_output
    assert "sample" in run_output and "summary" in run_output
    assert "hit" in run_output


def test_status_requires_module_and_points_to_overview(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["status"])
    assert "use 'varve ls'" in capsys.readouterr().err


@pytest.mark.parametrize("command", ["run", "reuse", "invalidate"])
def test_module_and_all_are_mutually_exclusive_and_required(command: str) -> None:
    with pytest.raises(SystemExit):
        main([command])
    for arguments in (("pkg.demo", "--all"), ("--all", "pkg.demo")):
        with pytest.raises(SystemExit) as exc_info:
            main([command, *arguments])
        assert exc_info.value.code == 2


@pytest.mark.parametrize("command", ["reuse", "invalidate"])
def test_top_level_review_forwards_repeatable_base_stage_targets(
    command: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_demo(tmp_path / "demo" / "out")
    captured = []

    def fake_review(context, *, decision, targets):
        captured.append((context.pipeline, decision, targets))
        return SourceReviewResult(
            decision=decision,
            stages=(),
        )

    monkeypatch.setattr("varve.cli.commands.execute_review", fake_review)
    assert (
        main(
            [
                command,
                CliDemo.__module__,
                "--root",
                str(tmp_path),
                "--stage",
                "summary",
                "--stage",
                "sample",
            ]
        )
        == 0
    )
    assert captured == [(CliDemo, command, ("summary", "sample"))]

    with pytest.raises(SystemExit):
        main([command, "--all", "--root", str(tmp_path), "--stage", "sample"])


def test_single_run_registers_pipeline_args_after_module_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _run_demo(tmp_path / "demo" / "out")
    captured = []

    def fake_run(context, **kwargs):
        captured.append((context, kwargs))
        return []

    monkeypatch.setattr("varve.cli.commands.execute_run", fake_run)
    assert (
        main(
            [
                "run",
                CliDemo.__module__,
                "--root",
                str(tmp_path),
                "--workers",
                "4",
                "--only",
                "sample",
                "--force",
            ]
        )
        == 0
    )
    assert captured[0][0].args == Args(workers=4)
    assert captured[0][1]["only"] == "sample"
    assert captured[0][1]["force"] is True

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "run",
                "--workers",
                "3",
                CliDemo.__module__,
                "--root",
                str(tmp_path),
                "--only",
                "sample",
            ]
        )
    assert exc_info.value.code == 2
    assert len(captured) == 1


def test_dynamic_args_require_module_first_and_keep_help_and_usage_consistent(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_demo(tmp_path / "demo" / "out")
    with pytest.raises(SystemExit) as help_exit:
        main(
            [
                "run",
                CliDemo.__module__,
                "--root",
                str(tmp_path),
                "--help",
            ]
        )
    assert help_exit.value.code == 0
    assert "--workers" in capsys.readouterr().out

    with pytest.raises(SystemExit) as static_help_exit:
        main(["run", "--help"])
    assert static_help_exit.value.code == 0
    static_help = capsys.readouterr().out
    assert "usage: varve run (MODULE [OPTIONS] | --all [OPTIONS])" in static_help
    assert "--workers" not in static_help

    with pytest.raises(SystemExit) as missing_exit:
        main(["run", "--workers", "4", "--root", str(tmp_path)])
    assert missing_exit.value.code == 2
    error = capsys.readouterr().err
    assert "requires exactly one of MODULE or --all" in error
    assert "Unknown module: 4" not in error

    with pytest.raises(SystemExit) as status_exit:
        main(["status", "--workers", "4", "--root", str(tmp_path)])
    assert status_exit.value.code == 2
    status_error = capsys.readouterr().err
    assert "use 'varve ls' for the overview" in status_error
    assert "Unknown module: 4" not in status_error


def test_dynamic_args_before_module_fail_and_after_module_execute_real_store(
    tmp_path: Path,
) -> None:
    entry = _run_demo(tmp_path / "demo" / "out")

    with pytest.raises(SystemExit) as exc_info:
        main(
            [
                "run",
                "--workers",
                "4",
                CliDemo.__module__,
                "--root",
                str(tmp_path),
                "--force",
            ]
        )
    assert exc_info.value.code == 2
    assert (entry.output_root / "sample.txt").read_text(encoding="utf-8") == "1"

    assert (
        main(
            [
                "run",
                CliDemo.__module__,
                "--root",
                str(tmp_path),
                "--workers",
                "5",
                "--force",
            ]
        )
        == 0
    )
    assert (entry.output_root / "sample.txt").read_text(encoding="utf-8") == "5"


def test_illegal_option_operand_is_not_imported_as_module(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _run_demo(tmp_path / "demo" / "out")
    imported = []
    monkeypatch.setattr(
        "varve.dashboard.cli.import_entry_pipeline",
        lambda entry: imported.append(entry) or CliDemo,
    )

    with pytest.raises(SystemExit) as exc_info:
        main(["run", "--bogus", "4", "--root", str(tmp_path)])
    assert exc_info.value.code == 2
    assert imported == []
    assert "Unknown module: 4" not in capsys.readouterr().err


def test_dynamic_value_matching_existing_module_and_typo_target_execute_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entry = _run_demo(tmp_path / "demo" / "out")
    executed = []
    monkeypatch.setattr(
        "varve.cli.commands.execute_run",
        lambda *args, **kwargs: executed.append((args, kwargs)) or [],
    )

    with pytest.raises(SystemExit) as ordering_error:
        main(
            [
                "run",
                "--label",
                CliDemo.__module__,
                "typo.module",
                "--root",
                str(tmp_path),
            ]
        )
    assert ordering_error.value.code == 2
    assert executed == []

    assert (
        main(
            [
                "run",
                "typo.module",
                "--root",
                str(tmp_path),
                "--label",
                CliDemo.__module__,
            ]
        )
        == 1
    )
    assert executed == []
    assert (entry.output_root / "sample.txt").read_text(encoding="utf-8") == "1"
    error = capsys.readouterr().err
    assert "Unknown module: typo.module" in error


def test_top_level_plan_does_not_construct_required_pipeline_args(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_base = tmp_path / "required" / "out"
    run(
        RequiredArgsDemo,
        Config(),
        args=RequiredArgs(workers=2),
        cli_out=output_base,
    )
    capsys.readouterr()

    assert RequiredArgsDemo.cli(["plan", "--out", str(output_base), "--only", "sample"]) == 0
    generated = capsys.readouterr().out.strip()
    assert (
        main(
            [
                "plan",
                RequiredArgsDemo.__module__,
                "--root",
                str(tmp_path),
                "--only",
                "sample",
            ]
        )
        == 0
    )
    top_level = capsys.readouterr().out.strip()
    assert generated == top_level == "sample"


def test_partial_matrix_summary_heading_matches_generated_and_top_level_status(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_base = tmp_path / "matrix" / "out"
    run(MatrixCliDemo, Config(), args=Args(), cli_out=output_base)
    capsys.readouterr()

    assert MatrixCliDemo.cli(["status", "score@model=y", "--out", str(output_base)]) == 0
    generated = capsys.readouterr().out
    assert (
        main(
            [
                "status",
                MatrixCliDemo.__module__,
                "--root",
                str(tmp_path),
                "--stage",
                "score@model=y",
            ]
        )
        == 0
    )
    top_level = capsys.readouterr().out
    assert "score@model=y  2 cells" in generated
    assert "score@model=y  2 cells" in top_level


def test_bulk_run_rejects_stage_force_and_pipeline_args(tmp_path: Path) -> None:
    for extra in (
        ["--only", "sample"],
        ["--force"],
        ["--workers", "4"],
    ):
        with pytest.raises(SystemExit):
            main(["run", "--all", "--root", str(tmp_path), *extra])


def test_top_level_rejects_identity_changing_options(tmp_path: Path) -> None:
    for option in ("--out", "--override", "--slice"):
        with pytest.raises(SystemExit):
            main(["run", CliDemo.__module__, "--root", str(tmp_path), option, "value"])


def test_overview_filters_before_exact_evaluation_and_status_after(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = [
        PipelineEntry(
            output_root=tmp_path / name,
            pipeline_id=name,
            pipeline_name="CliDemo",
            branch="main",
            module=f"pkg.{name}",
        )
        for name in ("hit", "review", "other")
    ]
    selected = entries[:2]
    checked = []
    monkeypatch.setattr(commands, "discover_scope", lambda *args, **kwargs: selected)

    def fake_load(entry, session):
        checked.append((entry.module, session))
        return _state(entry, "needs-review" if entry.module == "pkg.review" else "hit")

    monkeypatch.setattr(commands, "load_state", fake_load)
    assert (
        commands.overview_command(
            tmp_path,
            prefix="pkg.",
            branch="main",
            include_temp=False,
            rehash=False,
            statuses=("needs-review",),
        )
        == 0
    )
    assert [item[0] for item in checked] == ["pkg.hit", "pkg.review"]
    assert checked[0][1] is checked[1][1]
    output = capsys.readouterr().out
    assert "pkg.review" in output
    assert "pkg.hit" not in output


def test_overview_empty_discovery_is_failure_but_empty_status_is_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(commands, "discover_scope", lambda *args, **kwargs: [])
    assert (
        commands.overview_command(
            tmp_path,
            prefix="pkg",
            branch="main",
            include_temp=False,
            rehash=False,
            statuses=(),
        )
        == 1
    )
    assert "root=" in capsys.readouterr().err

    entry = PipelineEntry(
        output_root=tmp_path,
        pipeline_id="demo",
        pipeline_name="Demo",
        branch="main",
        module="pkg.demo",
    )
    monkeypatch.setattr(commands, "discover_scope", lambda *args, **kwargs: [entry])
    monkeypatch.setattr(commands, "load_state", lambda entry, session: _state(entry, "hit"))
    assert (
        commands.overview_command(
            tmp_path,
            prefix=None,
            branch=None,
            include_temp=False,
            rehash=False,
            statuses=("failed",),
        )
        == 0
    )
    assert "No pipelines match the selected statuses." in capsys.readouterr().out


def test_narrow_overview_keeps_complete_module_on_its_own_line(tmp_path: Path) -> None:
    module = "studies.exp.metric_eval.benchmark_misjudgment.run"
    entry = PipelineEntry(
        output_root=tmp_path,
        pipeline_id="short",
        pipeline_name="Demo",
        branch="main",
        module=module,
    )
    buffer = StringIO()
    console = Console(file=buffer, width=45, force_terminal=False)
    render_overview([_state(entry, "needs-review")], console=console)
    output = buffer.getvalue()
    assert module in output
    assert "…" not in output
    assert "needs-review" in output


def test_overview_error_row_does_not_hide_later_pipeline(tmp_path: Path) -> None:
    first = PipelineEntry(
        output_root=tmp_path / "broken",
        pipeline_id="broken",
        pipeline_name="Broken",
        branch="main",
        module="pkg.broken",
    )
    second = first.model_copy(
        update={"output_root": tmp_path / "good", "pipeline_id": "good", "module": "pkg.good"}
    )
    buffer = StringIO()
    render_overview(
        [_state(first, "error", reason="cannot import"), _state(second, "hit")],
        console=Console(file=buffer, width=120, force_terminal=False),
    )
    output = buffer.getvalue()
    assert "pkg.broken" in output and "error" in output
    assert "pkg.good" in output and "hit" in output


def test_non_tty_loading_emits_no_spinner() -> None:
    buffer = StringIO()
    console = Console(file=buffer, force_terminal=False)
    with commands._loading(console, "Discovering pipelines…") as loading:
        assert loading is None
    assert buffer.getvalue() == ""


def test_bulk_review_continues_after_entry_failure_and_uses_default_args(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = [
        PipelineEntry(
            output_root=tmp_path / name,
            pipeline_id=name,
            pipeline_name="CliDemo",
            branch="main",
            module=f"pkg.{name}",
        )
        for name in ("good", "other", "bad")
    ]
    monkeypatch.setattr(commands, "discover_scope", lambda *args, **kwargs: entries)

    def fake_import(entry):
        if entry.pipeline_id == "bad":
            raise RuntimeError("locked")
        return CliDemo

    monkeypatch.setattr(commands, "import_entry_pipeline", fake_import)
    seen_args = []

    def fake_context(entry, pipeline, args):
        seen_args.append(args)
        return type(
            "Context",
            (),
            {
                "args": args,
                "resolved": type(
                    "Resolved",
                    (),
                    {
                        "config": Config(),
                        "output_base": tmp_path,
                        "branch": "main",
                        "is_temporary": False,
                        "axes": None,
                    },
                )(),
                "graph": pipeline.graph(),
            },
        )()

    monkeypatch.setattr(commands, "resolve_entry_context", fake_context)
    refreshes = []
    created_sessions = []

    class Session:
        def __init__(self):
            created_sessions.append(self)

        def refresh_observations(self):
            refreshes.append(True)

    monkeypatch.setattr(commands, "_KeyingSession", Session)
    result = SourceReviewResult(
        decision="reuse",
        stages=(),
    )
    backend_sessions = []

    def fake_review(*args, **kwargs):
        backend_sessions.append(kwargs["_keying_session"])
        return result

    monkeypatch.setattr(commands, "execute_review", fake_review)

    assert (
        commands.bulk_review_command(
            tmp_path,
            prefix=None,
            branch=None,
            include_temp=False,
            decision="reuse",
        )
        == 1
    )
    assert seen_args == [Args(), Args()]
    assert len(created_sessions) == 1
    assert backend_sessions == [created_sessions[0], created_sessions[0]]
    assert len(refreshes) == 3
    assert "pkg.bad" in capsys.readouterr().out


def test_bulk_run_skips_hit_and_review_runs_eligible_then_rechecks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    entries = [
        PipelineEntry(
            output_root=tmp_path / name,
            pipeline_id=name,
            pipeline_name="CliDemo",
            branch="main",
            module=f"pkg.{name}",
        )
        for name in ("hit", "review", "stale")
    ]
    monkeypatch.setattr(commands, "discover_scope", lambda *args, **kwargs: entries)
    reads: dict[str, int] = {}

    def fake_load(entry, session):
        reads[entry.pipeline_id] = reads.get(entry.pipeline_id, 0) + 1
        if entry.pipeline_id == "hit":
            return _state(entry, "hit")
        if entry.pipeline_id == "review":
            return _state(entry, "needs-review")
        return _state(entry, "needs-run" if reads[entry.pipeline_id] == 1 else "hit")

    monkeypatch.setattr(commands, "load_state", fake_load)
    monkeypatch.setattr(commands, "import_entry_pipeline", lambda entry: CliDemo)
    monkeypatch.setattr(commands, "resolve_entry_context", lambda *args: object())
    ran = []
    monkeypatch.setattr(commands, "_run_context", lambda context, rehash: ran.append(context))
    refreshes = []

    class Session:
        def __init__(self, **kwargs):
            pass

        def refresh_observations(self):
            refreshes.append(True)

    monkeypatch.setattr(commands, "_KeyingSession", Session)

    assert (
        commands.bulk_run_command(
            tmp_path,
            prefix=None,
            branch=None,
            include_temp=False,
            rehash=False,
        )
        == 2
    )
    assert len(ran) == 1
    assert reads == {"hit": 1, "review": 1, "stale": 2}
    assert len(refreshes) == 2
    output = capsys.readouterr().out
    assert "TO REVIEW" in output
    assert "pkg.review" in output


def test_bulk_run_mixed_failure_returns_one_and_preserves_all_groups(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    review = PipelineEntry(
        output_root=tmp_path / "review",
        pipeline_id="review",
        pipeline_name="CliDemo",
        branch="main",
        module="pkg.review",
    )
    failed = review.model_copy(
        update={"output_root": tmp_path / "failed", "pipeline_id": "failed", "module": "pkg.failed"}
    )
    monkeypatch.setattr(commands, "discover_scope", lambda *args, **kwargs: [review, failed])
    monkeypatch.setattr(
        commands,
        "load_state",
        lambda entry, session: _state(
            entry, "needs-review" if entry.pipeline_id == "review" else "failed"
        ),
    )
    monkeypatch.setattr(commands, "import_entry_pipeline", lambda entry: CliDemo)
    monkeypatch.setattr(commands, "resolve_entry_context", lambda *args: object())
    monkeypatch.setattr(commands, "_run_context", lambda *args, **kwargs: None)

    assert (
        commands.bulk_run_command(
            tmp_path,
            prefix=None,
            branch=None,
            include_temp=False,
            rehash=False,
        )
        == 1
    )
    output = capsys.readouterr().out
    assert "TO REVIEW" in output
    assert "FAILED" in output
