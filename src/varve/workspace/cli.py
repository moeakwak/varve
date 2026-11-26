"""The top-level ``varve`` command: workspace overview and macro orchestration.

``varve <cmd> [MODULE] [--stage SELECTOR]``. ``ls`` and ``ui`` never import
user modules; the other commands import the pipelines they address. MODULE
and TARGET use selector rules (full name, module, or unambiguous dotted
suffix; ``MODULE@BRANCH`` picks one branch).
"""

from __future__ import annotations

import argparse
import shlex
import sqlite3
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import ValidationError
from rich import box as rich_box
from rich.console import Console
from rich.table import Table
from rich.text import Text

from varve.log import configure_cli_logging
from varve.style import STATUS_STYLES, format_ms, format_timestamp, make_console, status_text
from varve.workspace.config import WorkspaceSettings, load_workspace
from varve.workspace.discovery import Entry, discover

if TYPE_CHECKING:  # the engine and macro modules load only for commands that need them
    from varve.pipeline import Pipeline
    from varve.workspace.macro import MacroPlan, MacroResult, Node, Workspace


def user_errors() -> tuple[type[BaseException], ...]:
    """Errors reported as one line; importing them loads the engine, so only on demand."""

    from varve.cli.app import USER_ERRORS
    from varve.workspace.macro import MacroError

    return (*USER_ERRORS, MacroError)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "-v", "--verbose", action="store_true", default=argparse.SUPPRESS, help="Verbose logging."
    )
    parser = argparse.ArgumentParser(
        prog="varve",
        parents=[common],
        description="Workspace overview and cross-pipeline orchestration for varve pipelines.",
    )
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    commands.add_parser(
        "ls", parents=[common], help="List workspace pipelines and branches (zero-import)."
    )

    status = commands.add_parser("status", parents=[common], help="Stage decisions of a pipeline.")
    status.add_argument("module", metavar="MODULE")
    status.add_argument("--stage", metavar="STAGE[@AXIS=ID,...]")
    _branch(status)
    status.add_argument("--expand", action="store_true", help="List every unit.")
    status.add_argument("--rehash", action="store_true", help="Verify artifacts by content hash.")

    plan = commands.add_parser(
        "plan", parents=[common], help="Macro plan: ordered (pipeline, branch) nodes."
    )
    plan.add_argument("targets", nargs="*", metavar="TARGET", help="MODULE or MODULE@BRANCH.")
    _selection(plan)

    run = commands.add_parser(
        "run", parents=[common], help="Run (pipeline, branch) nodes in order."
    )
    run.add_argument("targets", nargs="*", metavar="TARGET", help="MODULE or MODULE@BRANCH.")
    _selection(run)
    run.add_argument(
        "--only", action="append", default=[], metavar="TARGET", help="Run only these nodes."
    )
    run.add_argument(
        "--skip", action="append", default=[], metavar="TARGET", help="Do not run these nodes."
    )
    run.add_argument("--from", dest="start", metavar="TARGET", help="Resume from this node.")
    run.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="FIELD=VALUE",
        help="Args field for every selected pipeline that declares it.",
    )
    run.add_argument("--force", "-f", action="store_true", help="Rerun every selected unit.")
    run.add_argument("--rehash", action="store_true", help="Re-hash inputs and artifacts.")
    run.add_argument("--no-tui", action="store_true", help="Plain log output.")
    run.add_argument("--dry-run", action="store_true", help="Print the plan without running.")

    reuse = commands.add_parser("reuse", parents=[common], help="Accept a stage's source change.")
    reuse.add_argument("module", metavar="MODULE")
    reuse.add_argument("--stage", required=True, metavar="STAGE")
    _branch(reuse)

    inspect = commands.add_parser("inspect", parents=[common], help="Explain one unit.")
    inspect.add_argument("module", metavar="MODULE")
    inspect.add_argument("--stage", required=True, metavar="STAGE[@AXIS=ID,...]")
    _branch(inspect)
    inspect.add_argument("--sources", action="store_true", help="List the stage's source closure.")
    inspect.add_argument("--log", action="store_true", help="Print the unit's captured log.")

    clean = commands.add_parser("clean", parents=[common], help="Remove managed outputs.")
    clean.add_argument("module", metavar="MODULE")
    clean.add_argument("--branch", metavar="NAME", help="Only this branch.")
    clean.add_argument("--storage", metavar="NAME", help="Only this storage.")
    clean.add_argument("--orphans", action="store_true", help="Only orphaned units.")
    clean.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation.")

    ui = commands.add_parser("ui", parents=[common], help="Serve the local read-only web UI.")
    ui.add_argument("--port", type=int, default=8765, help="Port (default 8765).")
    ui.add_argument("--host", default="127.0.0.1", help="Host (default 127.0.0.1).")
    return parser


def _branch(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--branch", default="main", metavar="NAME", help="Branch (default: main).")


def _selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--all", action="store_true", help="Include manual branches of the selected pipelines."
    )
    parser.add_argument("--branch", metavar="NAME", help="Only this branch of each pipeline.")


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    parser = build_parser()
    ns = parser.parse_args(raw)
    command = ns.command or "ls"
    configure_cli_logging(getattr(ns, "verbose", False), quiet=command != "run")
    console = make_console()
    errors = make_console(stderr=True)
    try:
        settings = load_workspace()
        return _dispatch(command, ns, raw, settings, console)
    except ValidationError as error:
        errors.print(f"[red]error:[/] invalid configuration or arguments\n{error}", soft_wrap=True)
        return 1
    except Exception as error:
        if not isinstance(error, user_errors()):
            raise
        errors.print("[red]error:[/] ", end="")
        errors.print(str(error), markup=False, highlight=False, soft_wrap=True)
        return 1


def _dispatch(
    command: str,
    ns: argparse.Namespace,
    raw: list[str],
    settings: WorkspaceSettings,
    console: Console,
) -> int:
    if command == "ls":
        render_workspace(console, settings, discover(settings, summaries=True))
        return 0
    if command == "ui":
        return _ui(settings, host=ns.host, port=ns.port)
    from varve.cli import render
    from varve.engine.service import (
        inspect_pipeline,
        parse_unit_selector,
        probe_pipeline,
        reuse_stage,
    )
    from varve.workspace.macro import Workspace, build_plan, parse_sets
    from varve.workspace.refs import WorkspaceRefResolver

    workspace = Workspace(settings)
    if command in ("plan", "run"):
        plan = build_plan(
            workspace,
            ns.targets,
            all_branches=ns.all,
            branch=ns.branch,
            only=getattr(ns, "only", ()),
            skip=getattr(ns, "skip", ()),
            start=getattr(ns, "start", None),
            sets=parse_sets(getattr(ns, "set", ())),
        )
        if command == "plan":
            return _plan(console, workspace, plan, ns.targets)
        return _run(console, workspace, plan, ns, raw)

    entry = workspace.resolve(ns.module)
    pipeline = workspace.pipeline(entry)
    common: dict[str, Any] = {
        "root": workspace.root_for(entry, pipeline),
        "workspace": settings,
        "refs": WorkspaceRefResolver(settings),
    }
    if command == "status":
        stage, coords = parse_unit_selector(ns.stage) if ns.stage else (None, {})
        view = probe_pipeline(
            pipeline,
            branch=ns.branch,
            stages=[stage] if stage else None,
            rehash=ns.rehash,
            expand=bool(ns.expand or stage),
            **common,
        )
        render.render_status(console, view, coords=coords, expand=ns.expand or bool(stage))
        return 0
    if command == "inspect":
        result = inspect_pipeline(pipeline, ns.stage, branch=ns.branch, **common)
        render.render_inspect(console, result, sources=ns.sources, log=ns.log)
        return 0
    if command == "reuse":
        reused = reuse_stage(pipeline, ns.stage, branch=ns.branch, **common)
        render.render_reuse(console, reused)
        return 0
    assert command == "clean"
    return _clean(console, pipeline, ns, common["root"], settings)


# -- ls ------------------------------------------------------------------------


def render_workspace(console: Console, settings: WorkspaceSettings, entries: list[Entry]) -> None:
    title = Text("Workspace", style="bold")
    if settings.config_path is not None:
        title.append(f" · {settings.config_path}", style="dim")
    if settings.root_base is not None:
        title.append(f" · VARVE_ROOT_BASE={settings.root_base}", style="yellow")
    console.print(title)
    if not entries:
        console.print(
            Text(f"No pipelines registered or found under {settings.scan_root}.", style="dim")
        )
        return
    table = Table(box=rich_box.SIMPLE_HEAD, show_edge=False)
    # Long names and paths fold rather than push the other columns off a narrow terminal.
    for column in ("Pipeline", "Branch", "Units", "Last run", "Root"):
        table.add_column(column, overflow="fold", min_width=8 if column != "Units" else None)
    for entry in entries:
        name = Text(entry.name, style="bold" if entry.entry else "")
        root = str(entry.root) if entry.root is not None else "-"
        if entry.error is not None:
            table.add_row(name, "-", Text(f"error: {entry.error}", style="red"), "-", root)
            continue
        if not entry.initialized:
            table.add_row(name, "-", status_text("uninitialized"), "-", root)
            continue
        if not entry.branches:
            table.add_row(name, "-", Text("no branches", style="dim"), "-", root)
            continue
        for index, branch in enumerate(entry.branches):
            label = Text(branch.name)
            if branch.is_temp:
                label.append(" (temp)", style="dim")
            elif branch.manual:
                label.append(" (manual)", style="dim")
            units = Text(", ".join(f"{n} {s}" for s, n in sorted(branch.counts.items())) or "-")
            last = Text("-")
            if branch.last_status is not None:
                last = status_text(branch.last_status) + Text(
                    f" {format_timestamp(branch.last_started_at)} · {format_ms(branch.last_wall_ms)}",
                    style="dim",
                )
            table.add_row(
                name if index == 0 else Text(""), label, units, last, root if index == 0 else ""
            )
    console.print(table)


# -- plan / run ----------------------------------------------------------------


def _probe_summary(
    workspace: Workspace, node: Node, upstream_pending: list[str]
) -> tuple[Text, bool]:
    """(summary, all hit) of one node's read-only probe."""

    from varve.engine.service import probe_pipeline
    from varve.workspace.refs import WorkspaceRefResolver

    try:
        view = probe_pipeline(
            node.pipeline,
            branch=node.branch,
            root=workspace.root_for(node.entry, node.pipeline),
            refs=WorkspaceRefResolver(workspace.settings),
            workspace=workspace.settings,
        )
    except user_errors() as error:
        if upstream_pending:
            return Text(f"waits for {', '.join(upstream_pending)}", style="dim"), False
        return Text(f"error: {error}", style="red"), False
    counts: dict[str, int] = {}
    for stage in view.stages:
        counts[stage.status] = counts.get(stage.status, 0) + 1
    text = Text()
    for index, (status, count) in enumerate(sorted(counts.items())):
        if index:
            text.append(" · ", style="dim")
        text.append(f"{count} {status}", style=STATUS_STYLES.get(status, ""))
    all_hit = set(counts) <= {"hit"}
    if upstream_pending and not all_hit:
        text.append(f" (after {', '.join(upstream_pending)})", style="dim")
    elif upstream_pending:
        text.append(f" (may change after {', '.join(upstream_pending)})", style="dim")
    return text, all_hit


def _plan_table() -> Table:
    table = Table(box=rich_box.SIMPLE_HEAD, show_edge=False)
    for column in ("#", "Node", "Why", "Reads"):
        table.add_column(column, no_wrap=column != "Reads")
    return table


def _plan(console: Console, workspace: Workspace, plan: MacroPlan, targets: list[str]) -> int:
    from varve.cli import render
    from varve.engine.service import probe_pipeline
    from varve.workspace.refs import WorkspaceRefResolver

    console.print(Text("Plan", style="bold") + Text(f" · {len(plan.nodes)} node(s)", style="dim"))
    table = _plan_table()
    table.add_column("Probe")
    fresh: dict[str, bool] = {}
    for index, node in enumerate(plan.nodes, 1):
        pending = [label for label in plan.upstream[node.label] if not fresh.get(label, False)]
        summary, all_hit = _probe_summary(workspace, node, pending)
        fresh[node.label] = all_hit and not pending
        table.add_row(
            str(index),
            Text(node.label, style="bold"),
            plan.why[node.label],
            ", ".join(plan.upstream[node.label]) or "-",
            summary,
        )
    console.print(table)
    console.print(
        Text(
            "Each node is probed against its upstreams' committed records; "
            "'after X' marks decisions that X's run may change.",
            style="dim",
        )
    )
    if len(targets) == 1:
        module = targets[0].partition("@")[0]
        entry = workspace.resolve(module)
        for node in plan.nodes:
            if node.entry.name != entry.name:
                continue
            view = probe_pipeline(
                node.pipeline,
                branch=node.branch,
                root=workspace.root_for(node.entry, node.pipeline),
                refs=WorkspaceRefResolver(workspace.settings),
                workspace=workspace.settings,
            )
            console.print()
            render.render_plan(console, view, node.pipeline.graph())
    return 0


def _run(
    console: Console,
    workspace: Workspace,
    plan: MacroPlan,
    ns: argparse.Namespace,
    raw: list[str],
) -> int:
    if ns.dry_run:
        console.print(
            Text("Dry run", style="bold") + Text(f" · {len(plan.nodes)} node(s)", style="dim")
        )
        table = _plan_table()
        table.add_column("Action")
        table.add_column("--set")
        for index, node in enumerate(plan.nodes, 1):
            label = node.label
            action = (
                Text("run", style="bold") if label in plan.selected else Text("skip", style="dim")
            )
            sets = ", ".join(f"{k}={v!r}" for k, v in plan.sets.get(label, {}).items()) or "-"
            table.add_row(
                str(index),
                label,
                plan.why[label],
                ", ".join(plan.upstream[label]) or "-",
                action,
                sets,
            )
        console.print(table)
        return 0

    from varve.workspace.macro import run_plan

    def before(node: Node) -> None:
        console.rule(Text(node.label, style="bold"))

    result = run_plan(
        workspace,
        plan,
        force=ns.force,
        rehash=ns.rehash,
        tui=False if ns.no_tui else None,
        argv=["varve", *raw],
        before_node=before,
        errors=user_errors(),
    )
    render_macro_summary(console, result)
    if result.failed is not None:
        console.print(Text("Resume with:", style="bold"))
        console.print("  " + resume_command(raw, result.failed), markup=False, highlight=False)
    return result.exit_code


def resume_command(raw: Sequence[str], label: str) -> str:
    """The original ``varve run`` command with ``--from <label>``."""

    args: list[str] = []
    skip_next = False
    for item in raw:
        if skip_next:
            skip_next = False
            continue
        if item == "--from":
            skip_next = True
            continue
        if item.startswith("--from="):
            continue
        args.append(item)
    return shlex.join(["varve", *args, "--from", label])


def render_macro_summary(console: Console, result: MacroResult) -> None:
    table = Table(title="Summary", title_justify="left", box=rich_box.SIMPLE_HEAD, show_edge=False)
    for column in ("Node", "Status", "Executed", "Hit", "Wall", "Detail"):
        table.add_column(column, no_wrap=column != "Detail")
    total = 0
    for outcome in result.outcomes:
        total += outcome.wall_ms
        detail = outcome.error or ""
        if outcome.result is not None and outcome.status != "ok":
            bad = [s for s in outcome.result.stages.values() if s.status != "ok"]
            detail = "; ".join(f"{s.name}: {s.status}" for s in bad)
        ran = outcome.result is not None
        table.add_row(
            outcome.label,
            status_text(outcome.status),
            str(outcome.executed) if ran else "-",
            str(outcome.hit) if ran else "-",
            format_ms(outcome.wall_ms) if ran or outcome.status == "error" else "-",
            Text(detail, style="dim"),
        )
    console.print(table)
    console.print(Text(f"Total wall time {format_ms(total)}", style="dim"))


# -- clean / ui ----------------------------------------------------------------


def _clean(
    console: Console,
    pipeline: type[Pipeline],
    ns: argparse.Namespace,
    root: Path | None,
    settings: WorkspaceSettings,
) -> int:
    from varve.cli import render
    from varve.engine.service import clean_pipeline, plan_clean

    def call(function):
        return function(
            pipeline,
            root=root,
            branch=ns.branch,
            storage=ns.storage,
            orphans=ns.orphans,
            workspace=settings,
        )

    plan = call(plan_clean)
    if not plan.paths and not plan.rows:
        console.print(f"Nothing to clean for {plan.description}.")
        return 0
    render.render_clean_plan(console, plan)
    if not ns.yes and not _confirm("Remove these?"):
        console.print("Aborted.")
        return 1
    done = call(clean_pipeline)
    console.print(f"Removed {len(done.paths)} path(s) and {done.rows} unit row(s).")
    return 0


def _confirm(message: str) -> bool:
    try:
        answer = input(f"{message} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def state_db_paths(settings: WorkspaceSettings) -> list[Path]:
    """Existing ``state.db`` paths of the workspace (registered and discovered)."""

    return [entry.db_path for entry in discover(settings) if entry.db_path is not None]


def _ui(settings: WorkspaceSettings, *, host: str, port: int) -> int:
    import varve.ui

    try:
        serve = varve.ui.serve
    except ImportError as error:
        errors = make_console(stderr=True)
        errors.print("[red]error:[/] ", end="")
        errors.print(str(error), markup=False, highlight=False, soft_wrap=True)
        return 1
    try:
        serve(lambda: state_db_paths(settings), host=host, port=port)
    except KeyboardInterrupt:
        pass
    except sqlite3.Error as error:  # pragma: no cover - defensive
        make_console(stderr=True).print(f"[red]error:[/] {error}")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
