"""Command-line interface for Pipeline subclasses."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel
from rich.table import Table

from varve.branch_config import resolve_branch
from varve.cli import argmap
from varve.cli.clean import clean
from varve.cli.details import render_details
from varve.details import collect_pipeline_details
from varve.engine.runner import StageOutcome, evaluate_state, run, selected_stages
from varve.log import configure_cli_logging
from varve.pipeline import Pipeline
from varve.style import make_console, status_text

_CONFIG_COMMANDS = {"run", "status", "details", "clean"}
_NEGATIVE_NUMBER_RE = re.compile(r"^-\d+$|^-\d*\.\d+$")
_COMMAND_OPTION_ARITIES = {
    "run": {
        "--branch": 1,
        "--override": 1,
        "--upto": 1,
        "--downstream": 1,
        "--force": 0,
        "-f": 0,
        "--out": 1,
    },
    "status": {"--branch": 1, "--upto": 1, "--downstream": 1, "--out": 1},
    "details": {
        "--branch": 1,
        "--out": 1,
        "--expand": 0,
        "--all": 0,
    },
    "clean": {
        "--branch": 1,
        "--downstream": 1,
        "--out": 1,
        "--yes": 0,
        "-y": 0,
    },
    "plan": {"--upto": 1, "--downstream": 1},
}


def _args_from_namespace(
    pipeline: type[Pipeline],
    namespace: argparse.Namespace,
) -> BaseModel:
    init_kwargs = argmap.collect_cli_args_namespace(namespace, pipeline.Args)
    return pipeline.Args.model_validate(init_kwargs)


def _print_list(pipeline: type[Pipeline]) -> None:
    table = Table(box=None)
    table.add_column("STAGE")
    table.add_column("KIND")
    table.add_column("NEEDS")
    for name in pipeline.topo_order():
        spec = pipeline.stages()[name]
        needs = ", ".join(spec.needs) if spec.needs else "-"
        kind = "batch" if spec.kind == "batch" else "stage"
        table.add_row(name, kind, needs)
    make_console().print(table)


def _format_elapsed(value: float | None) -> str:
    return f"{value:.2f}s" if value is not None else "-"


def _print_outcomes(outcomes: list[StageOutcome], *, elapsed: bool) -> None:
    table = Table(box=None)
    table.add_column("STAGE")
    table.add_column("STATUS")
    table.add_column("REASON")
    if elapsed:
        table.add_column("ELAPSED", justify="right")
    for outcome in outcomes:
        row = [outcome.stage, status_text(outcome.status), outcome.reason]
        if elapsed:
            row.append(_format_elapsed(outcome.elapsed))
        table.add_row(*row)
    make_console().print(table)


def _print_plan(
    pipeline: type[Pipeline],
    *,
    upto: str | None,
    downstream: str | None,
) -> None:
    selected = selected_stages(pipeline, upto=upto, downstream=downstream)
    print(" -> ".join(name for name in pipeline.topo_order() if name in selected))


def _print_status(
    pipeline: type[Pipeline],
    config,
    args,
    *,
    upto: str | None,
    downstream: str | None,
    cli_out: Path | None,
    branch: str,
    is_temporary: bool,
) -> None:
    outcomes = evaluate_state(
        pipeline,
        config,
        args=args,
        upto=upto,
        downstream=downstream,
        cli_out=cli_out,
        branch=branch,
        is_temporary=is_temporary,
    )
    _print_outcomes(outcomes, elapsed=False)


def _default_confirm(message: str) -> bool:
    try:
        answer = input(f"{message} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _selected_command_index(argv: list[str]) -> int | None:
    index = 0
    while index < len(argv):
        token = argv[index]
        if token in {"-v", "--verbose"}:
            index += 1
            continue
        if token == "--":
            next_index = index + 1
            return next_index if next_index < len(argv) else None
        if token.startswith("-"):
            return None
        return index
    return None


def _option_name(token: str) -> str:
    if token.startswith("--"):
        return token.split("=", 1)[0]
    return token


def _looks_like_option(token: str) -> bool:
    return token.startswith("-") and token != "-" and _NEGATIVE_NUMBER_RE.match(token) is None


def _has_unknown_option_before_config_registration(
    *,
    command: str,
    command_args: list[str],
    args_type: type[BaseModel],
) -> bool:
    option_arities = argmap.args_option_arities(args_type)
    option_arities.update(_COMMAND_OPTION_ARITIES[command])
    # Let argparse handle help instead of failing the strict precheck.
    option_arities.setdefault("--help", 0)
    option_arities.setdefault("-h", 0)

    index = 0
    while index < len(command_args):
        token = command_args[index]
        if token == "--":
            return False
        if not token.startswith("-") or token == "-":
            index += 1
            continue
        option = _option_name(token)
        arity = option_arities.get(option)
        if arity is None:
            return True
        index += 1
        if arity == 1 and "=" not in token:
            if index >= len(command_args) or _looks_like_option(command_args[index]):
                return True
            index += 1
    return False


def main(pipeline: type[Pipeline], argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    selected_command_index = _selected_command_index(raw_argv)
    selected_command = (
        raw_argv[selected_command_index] if selected_command_index is not None else None
    )
    parser = argparse.ArgumentParser(prog=pipeline.__name__)
    parser.add_argument("-v", "--verbose", action="store_true")
    subparsers = parser.add_subparsers(dest="command", required=True)

    out_help = (
        "Override the output base. For named yaml branches this replaces that branch's "
        "default base; for main and temporary branches it replaces the main base."
    )

    run_parser = subparsers.add_parser("run", help="run selected stages")
    run_parser.add_argument("--branch", default="main", metavar="NAME", help="Select a branch.")
    run_parser.add_argument(
        "--override",
        metavar="JSON",
        help="Merge JSON over main Config and run a temporary branch.",
    )
    run_stage = run_parser.add_mutually_exclusive_group()
    run_stage.add_argument("--upto", metavar="STAGE", help="Run STAGE and all upstream stages.")
    run_stage.add_argument(
        "--downstream", metavar="STAGE", help="Run STAGE and all downstream stages."
    )
    run_parser.add_argument(
        "--force", "-f", action="store_true", help="Ignore cache for selected stages."
    )
    run_parser.add_argument("--out", type=Path, metavar="PATH", help=out_help)

    status_parser = subparsers.add_parser("status", help="show read-only stage status")
    status_parser.add_argument("--branch", default="main", metavar="NAME", help="Select a branch.")
    status_stage = status_parser.add_mutually_exclusive_group()
    status_stage.add_argument("--upto", metavar="STAGE", help="Show STAGE and all upstream stages.")
    status_stage.add_argument(
        "--downstream", metavar="STAGE", help="Show STAGE and all downstream stages."
    )
    status_parser.add_argument("--out", type=Path, metavar="PATH", help=out_help)

    details_parser = subparsers.add_parser("details", help="show stage key details")
    details_parser.add_argument("stage", nargs="?", choices=pipeline.topo_order())
    details_parser.add_argument("--branch", default="main", metavar="NAME", help="Select a branch.")
    details_parser.add_argument("--out", type=Path, metavar="PATH", help=out_help)
    details_depth = details_parser.add_mutually_exclusive_group()
    details_depth.add_argument("--expand", action="store_true", help="Show one dependency level.")
    details_depth.add_argument("--all", action="store_true", help="Show the full dependency tree.")

    clean_parser = subparsers.add_parser("clean", help="delete selected store records and outputs")
    clean_parser.add_argument("--branch", default="main", metavar="NAME", help="Select a branch.")
    clean_parser.add_argument(
        "--downstream",
        metavar="STAGE",
        help="Clean STAGE and all downstream stages.",
    )
    clean_parser.add_argument("--out", type=Path, metavar="PATH", help=out_help)
    clean_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation.")

    plan_parser = subparsers.add_parser("plan", help="print selected stage order")
    plan_stage = plan_parser.add_mutually_exclusive_group()
    plan_stage.add_argument("--upto", metavar="STAGE", help="Print STAGE and all upstream stages.")
    plan_stage.add_argument(
        "--downstream", metavar="STAGE", help="Print STAGE and all downstream stages."
    )

    subparsers.add_parser("list")

    if selected_command in _CONFIG_COMMANDS and selected_command_index is not None:
        command_args = raw_argv[selected_command_index + 1 :]
        if _has_unknown_option_before_config_registration(
            command=selected_command,
            command_args=command_args,
            args_type=pipeline.Args,
        ):
            parser.error("unknown option or missing option value")

    if selected_command == "run":
        argmap.register_args(run_parser, pipeline.Args)
    if selected_command == "status":
        argmap.register_args(status_parser, pipeline.Args)
    if selected_command == "details":
        argmap.register_args(details_parser, pipeline.Args)
    if selected_command == "clean":
        argmap.register_args(clean_parser, pipeline.Args)

    namespace = parser.parse_args(raw_argv)
    configure_cli_logging(namespace.verbose, quiet=namespace.command != "run")

    if namespace.command == "list":
        _print_list(pipeline)
        return 0
    if namespace.command == "plan":
        _print_plan(pipeline, upto=namespace.upto, downstream=namespace.downstream)
        return 0

    resolved = resolve_branch(
        pipeline,
        branch=namespace.branch,
        override_json=namespace.override if namespace.command == "run" else None,
        cli_out=namespace.out,
        allow_bare_output_root=namespace.command == "clean",
    )
    config = resolved.config
    args = _args_from_namespace(pipeline, namespace)
    if namespace.command == "status":
        _print_status(
            pipeline,
            config,
            args,
            upto=namespace.upto,
            downstream=namespace.downstream,
            cli_out=resolved.output_base,
            branch=resolved.branch,
            is_temporary=resolved.is_temporary,
        )
    elif namespace.command == "details":
        details = collect_pipeline_details(
            pipeline,
            config,
            args=args,
            out=pipeline.output_root(
                config,
                cli_out=resolved.output_base,
                branch=resolved.branch,
                is_temporary=resolved.is_temporary,
            ),
            branch=resolved.branch,
            stage=namespace.stage,
        )
        depth = None if namespace.all else 1 if namespace.expand else 0
        render_details(make_console(), details, stage=namespace.stage, depth=depth)
    elif namespace.command == "clean":
        allowed_roots = (
            None if isinstance(config, SimpleNamespace) else pipeline.clean_roots(config)
        )
        clean(
            pipeline,
            config,
            cli_out=resolved.output_base,
            branch=resolved.branch,
            is_temporary=resolved.is_temporary,
            target=namespace.downstream,
            yes=namespace.yes,
            allowed_roots=allowed_roots,
            confirm=_default_confirm,
        )
    elif namespace.command == "run":
        outcomes = run(
            pipeline,
            config,
            args=args,
            upto=namespace.upto,
            downstream=namespace.downstream,
            force=namespace.force,
            cli_out=resolved.output_base,
            branch=resolved.branch,
            is_temporary=resolved.is_temporary,
            temporary_config=resolved.temporary_config,
        )
        _print_outcomes(outcomes, elapsed=True)
    return 0
