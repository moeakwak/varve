"""The generated command-line interface of a Pipeline (``Pipeline.cli()``)."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from pydantic import ValidationError

from varve.branch_config import parse_sample, parse_slice
from varve.cli import argmap, render
from varve.engine.probe import ProbeError
from varve.engine.refs import RefResolutionError
from varve.engine.resources import ResourceError
from varve.engine.run import RunOptions
from varve.engine.service import (
    clean_pipeline,
    inspect_pipeline,
    parse_unit_selector,
    plan_clean,
    probe_pipeline,
    reuse_stage,
    run_pipeline,
)
from varve.graph import GraphError
from varve.log import configure_cli_logging
from varve.pipeline import Pipeline
from varve.store.db import SchemaError
from varve.store.lock import LockBusyError
from varve.style import make_console

USER_ERRORS = (
    ValueError,
    GraphError,
    ProbeError,
    RefResolutionError,
    ResourceError,
    SchemaError,
    LockBusyError,
    FileNotFoundError,
    sqlite3.OperationalError,
)


def _common() -> argparse.ArgumentParser:
    # Accepted before or after the subcommand; SUPPRESS keeps a later default
    # from overwriting an earlier value.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--root",
        type=Path,
        default=argparse.SUPPRESS,
        metavar="PATH",
        help="Pipeline root holding .varve/ (default: the pipeline module's directory).",
    )
    common.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Verbose logging.",
    )
    return common


def _add_branch(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--branch", default="main", metavar="NAME", help="Branch (default: main).")


def _add_selection(parser: argparse.ArgumentParser, verb: str) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--only", metavar="STAGE", help=f"{verb} only this stage.")
    group.add_argument("--upto", metavar="STAGE", help=f"{verb} this stage and its ancestors.")
    group.add_argument(
        "--downstream", metavar="STAGE", help=f"{verb} this stage and its descendants."
    )


def build_parser(pipeline: type[Pipeline]) -> argparse.ArgumentParser:
    common = _common()
    parser = argparse.ArgumentParser(
        prog=_prog(pipeline),
        parents=[common],
        description=(pipeline.__doc__ or "").strip().splitlines()[0] if pipeline.__doc__ else None,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    run = commands.add_parser("run", parents=[common], help="Run stages whose units are not hits.")
    _add_branch(run)
    run.add_argument(
        "--override", metavar="JSON", help="Merge JSON over main's Config (temporary branch)."
    )
    run.add_argument(
        "--temp", action="store_true", help="Run in a temporary branch derived from --branch."
    )
    _add_selection(run, "Run")
    run.add_argument("--force", "-f", action="store_true", help="Rerun selected stages' units.")
    run.add_argument(
        "--rehash",
        action="store_true",
        help="Re-hash inputs and artifacts instead of trusting stat.",
    )
    run.add_argument(
        "--failed-only", action="store_true", help="Only rerun units whose last run failed."
    )
    run.add_argument(
        "--slice",
        action="append",
        default=[],
        metavar="AXIS=IDS",
        help="Restrict an axis to ids (v1,v2 or @file); needs --temp or a new --branch.",
    )
    run.add_argument(
        "--sample",
        action="append",
        default=[],
        metavar="AXIS=N",
        help="Deterministically sample N ids of an axis; needs --temp or a new --branch.",
    )
    run.add_argument("--seed", type=int, default=0, help="Seed for --sample (default 0).")
    run.add_argument(
        "--no-tui", action="store_true", help="Plain log output instead of the live TUI."
    )
    argmap.register_args(run, pipeline.Args)

    status = commands.add_parser("status", parents=[common], help="Show stage and unit decisions.")
    status.add_argument("selector", nargs="?", metavar="STAGE[@AXIS=ID,...]")
    _add_branch(status)
    status.add_argument("--expand", action="store_true", help="List every unit.")
    status.add_argument("--rehash", action="store_true", help="Verify artifacts by content hash.")
    argmap.register_args(status, pipeline.Args)

    plan = commands.add_parser("plan", parents=[common], help="Show the stage DAG with decisions.")
    _add_selection(plan, "Show")
    _add_branch(plan)
    plan.add_argument("--rehash", action="store_true", help="Verify artifacts by content hash.")
    argmap.register_args(plan, pipeline.Args)

    commands.add_parser("ls", parents=[common], help="List stages and branches.")

    clean = commands.add_parser("clean", parents=[common], help="Remove managed outputs.")
    clean.add_argument("--branch", metavar="NAME", help="Only this branch.")
    clean.add_argument("--storage", metavar="NAME", help="Only this storage.")
    clean.add_argument("--orphans", action="store_true", help="Only orphaned units.")
    clean.add_argument("--yes", "-y", action="store_true", help="Do not ask for confirmation.")

    inspect = commands.add_parser("inspect", parents=[common], help="Explain one unit.")
    inspect.add_argument("selector", metavar="STAGE[@AXIS=ID,...]")
    _add_branch(inspect)
    inspect.add_argument("--sources", action="store_true", help="List the stage's source closure.")
    inspect.add_argument("--log", action="store_true", help="Print the unit's captured log.")

    reuse = commands.add_parser("reuse", parents=[common], help="Accept a stage's source change.")
    reuse.add_argument("stage", metavar="STAGE")
    _add_branch(reuse)
    return parser


def _prog(pipeline: type[Pipeline]) -> str:
    module = pipeline.import_module_name()
    if module == "__main__":
        return Path(sys.argv[0]).name if sys.argv and sys.argv[0] else pipeline.__name__
    return f"python -m {module}"


def main(pipeline: type[Pipeline], argv: list[str] | None = None) -> int:
    raw = list(argv) if argv is not None else sys.argv[1:]
    parser = build_parser(pipeline)
    ns = parser.parse_args(raw)
    verbose = getattr(ns, "verbose", False)
    configure_cli_logging(verbose, quiet=ns.command != "run")
    console = make_console()
    errors = make_console(stderr=True)
    root: Path | None = getattr(ns, "root", None)
    try:
        return _dispatch(pipeline, ns, raw, root, console)
    except ValidationError as error:
        errors.print(f"[red]error:[/] invalid configuration or arguments\n{error}", soft_wrap=True)
        return 1
    except USER_ERRORS as error:
        errors.print("[red]error:[/] ", end="")
        errors.print(str(error), markup=False, highlight=False, soft_wrap=True)
        return 1


def _dispatch(
    pipeline: type[Pipeline], ns: argparse.Namespace, raw: list[str], root: Path | None, console
) -> int:
    command = ns.command
    if command == "ls":
        render.render_ls(console, pipeline, root)
        return 0
    if command == "clean":
        return _clean(pipeline, ns, root, console)
    if command == "reuse":
        result = reuse_stage(pipeline, ns.stage, branch=ns.branch, root=root)
        render.render_reuse(console, result)
        return 0
    if command == "inspect":
        result = inspect_pipeline(pipeline, ns.selector, branch=ns.branch, root=root)
        render.render_inspect(console, result, sources=ns.sources, log=ns.log)
        return 0

    args = argmap.model_from_namespace(ns, pipeline.Args)
    if command == "status":
        stage, coords = parse_unit_selector(ns.selector) if ns.selector else (None, {})
        view = probe_pipeline(
            pipeline,
            branch=ns.branch,
            stages=[stage] if stage else None,
            args=args,
            root=root,
            rehash=ns.rehash,
            expand=bool(ns.expand or stage),
        )
        render.render_status(console, view, coords=coords, expand=ns.expand or bool(stage))
        return 0
    if command == "plan":
        view = probe_pipeline(pipeline, branch=ns.branch, args=args, root=root, rehash=ns.rehash)
        render.render_plan(
            console, view, pipeline.graph(), only=ns.only, upto=ns.upto, downstream=ns.downstream
        )
        return 0

    assert command == "run"
    slices = dict(parse_slice(text, base=Path.cwd()) for text in ns.slice)
    samples = dict(parse_sample(text) for text in ns.sample)
    options = RunOptions(
        only=ns.only,
        upto=ns.upto,
        downstream=ns.downstream,
        force=ns.force,
        rehash=ns.rehash,
        failed_only=ns.failed_only,
        tui=False if ns.no_tui else None,
        argv=[_prog(pipeline), *raw],
    )
    result = run_pipeline(
        pipeline,
        branch=ns.branch,
        overrides=ns.override,
        args=args,
        options=options,
        root=root,
        temp=ns.temp,
        slices=slices,
        samples=samples,
        seed=ns.seed,
    )
    render.render_run_summary(console, result)
    return result.exit_code


def _clean(pipeline: type[Pipeline], ns: argparse.Namespace, root: Path | None, console) -> int:
    def call(function):
        return function(
            pipeline, root=root, branch=ns.branch, storage=ns.storage, orphans=ns.orphans
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
