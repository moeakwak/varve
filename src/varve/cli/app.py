"""The generated command-line interface of a Pipeline (``Pipeline.cli()``)."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from varve.cli import argmap, common, render
from varve.log import configure_cli_logging
from varve.pipeline import Pipeline
from varve.style import make_console


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


def _add_selection(parser: argparse.ArgumentParser, verb: str) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--only", metavar="STAGE", help=f"{verb} only this stage.")
    group.add_argument("--upto", metavar="STAGE", help=f"{verb} this stage and its ancestors.")
    group.add_argument(
        "--downstream", metavar="STAGE", help=f"{verb} this stage and its descendants."
    )


def build_parser(pipeline: type[Pipeline]) -> argparse.ArgumentParser:
    shared = _common()
    parser = argparse.ArgumentParser(
        prog=_prog(pipeline),
        parents=[shared],
        description=(pipeline.__doc__ or "").strip().splitlines()[0] if pipeline.__doc__ else None,
    )
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    run = commands.add_parser("run", parents=[shared], help="Run stages whose units are not hits.")
    common.add_branch(run)
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

    status = commands.add_parser("status", parents=[shared], help="Show stage and unit decisions.")
    status.add_argument("selector", nargs="?", metavar="STAGE[@AXIS=ID,...]")
    common.add_status_args(status)
    argmap.register_args(status, pipeline.Args)

    plan = commands.add_parser("plan", parents=[shared], help="Show the stage DAG with decisions.")
    _add_selection(plan, "Show")
    common.add_branch(plan)
    plan.add_argument("--rehash", action="store_true", help="Verify artifacts by content hash.")
    argmap.register_args(plan, pipeline.Args)

    commands.add_parser("ls", parents=[shared], help="List stages and branches.")

    clean = commands.add_parser("clean", parents=[shared], help="Remove managed outputs.")
    common.add_clean_args(clean)

    inspect = commands.add_parser("inspect", parents=[shared], help="Explain one unit.")
    inspect.add_argument("selector", metavar="STAGE[@AXIS=ID,...]")
    common.add_inspect_args(inspect)

    reuse = commands.add_parser("reuse", parents=[shared], help="Accept a stage's source change.")
    reuse.add_argument("stage", metavar="STAGE")
    common.add_branch(reuse)
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
    root: Path | None = getattr(ns, "root", None)
    return common.report_errors(lambda: _dispatch(pipeline, ns, raw, root, make_console()))


def _dispatch(
    pipeline: type[Pipeline], ns: argparse.Namespace, raw: list[str], root: Path | None, console
) -> int:
    from varve.engine.service import probe_pipeline, run_pipeline

    command = ns.command
    if command == "ls":
        render.render_ls(console, pipeline, root)
        return 0
    if command == "clean":
        return common.clean_command(console, pipeline, ns, root=root)
    if command == "reuse":
        return common.reuse_command(console, pipeline, ns.stage, ns, root=root)
    if command == "inspect":
        return common.inspect_command(console, pipeline, ns.selector, ns, root=root)

    args = argmap.model_from_namespace(ns, pipeline.Args)
    if command == "status":
        return common.status_command(console, pipeline, ns.selector, ns, args=args, root=root)
    if command == "plan":
        view = probe_pipeline(pipeline, branch=ns.branch, args=args, root=root, rehash=ns.rehash)
        render.render_plan(
            console, view, pipeline.graph(), only=ns.only, upto=ns.upto, downstream=ns.downstream
        )
        return 0

    assert command == "run"
    from varve.branch_config import parse_sample, parse_slice
    from varve.engine.run import RunOptions

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
