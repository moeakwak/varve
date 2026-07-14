"""Command-line interface for Pipeline subclasses."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from pydantic import BaseModel

from varve.branch_config import resolve_branch
from varve.cli import argmap
from varve.cli.clean import default_confirm
from varve.cli.commands import dispatch_command
from varve.cli.structure import render_structure
from varve.command import resolved_command_context
from varve.log import configure_cli_logging
from varve.matrix import build_graph
from varve.pipeline import Pipeline
from varve.style import make_console

_NEGATIVE_NUMBER_RE = re.compile(r"^-\d+$|^-\d*\.\d+$")


def _selected_command_index(argv: list[str]) -> int | None:
    for index, token in enumerate(argv):
        if token in {"-v", "--verbose"}:
            continue
        if token == "--":
            next_index = index + 1
            return next_index if next_index < len(argv) else None
        if token.startswith("-"):
            return None
        return index
    return None


def _looks_like_option(token: str) -> bool:
    return token.startswith("-") and token != "-" and _NEGATIVE_NUMBER_RE.match(token) is None


def _has_unknown_option_before_config_registration(
    *,
    parser: argparse.ArgumentParser,
    command_args: list[str],
    args_type: type[BaseModel],
) -> bool:
    option_arities = argmap.args_option_arities(args_type)
    option_arities.update(
        (option, 0 if action.nargs == 0 else 1)
        for action in parser._actions
        for option in action.option_strings
    )

    index = 0
    while index < len(command_args):
        token = command_args[index]
        if token == "--":
            return False
        if not token.startswith("-") or token == "-":
            index += 1
            continue
        option = token.split("=", 1)[0] if token.startswith("--") else token
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
    selector_help = argmap.STAGE_SELECTOR_HELP
    argmap.add_stage_selection(run_parser, selector_help, verb="Run")
    run_parser.add_argument(
        "--slice", action="append", default=[], metavar="AXIS=ID", help="Slice a temporary branch."
    )
    run_parser.add_argument(
        "--force", "-f", action="store_true", help="Ignore cache for selected stages."
    )
    run_parser.add_argument(
        "--rehash", action="store_true", help="Ignore persisted stat shortcuts while keying."
    )
    run_view = run_parser.add_mutually_exclusive_group()
    run_view.add_argument(
        "--expand", action="store_true", help="Show every selected concrete matrix cell."
    )
    run_view.add_argument(
        "--compact", action="store_true", help="Fold selected cells by matrix stage."
    )
    run_parser.add_argument("--out", type=Path, metavar="PATH", help=out_help)

    status_parser = subparsers.add_parser("status", help="show pipeline and stage status")
    status_parser.add_argument(
        "stage",
        nargs="?",
        metavar="STAGE_SELECTOR",
        help=selector_help,
    )
    status_parser.add_argument("--branch", default="main", metavar="NAME", help="Select a branch.")
    status_parser.add_argument("--out", type=Path, metavar="PATH", help=out_help)
    status_parser.add_argument(
        "--rehash", action="store_true", help="Ignore persisted stat shortcuts while keying."
    )
    status_display = status_parser.add_mutually_exclusive_group()
    status_display.add_argument(
        "--expand",
        action="store_true",
        help="Show concrete matrix cells or detailed stage state.",
    )

    clean_parser = subparsers.add_parser("clean", help="delete selected store records and outputs")
    clean_parser.add_argument("--branch", default="main", metavar="NAME", help="Select a branch.")
    clean_parser.add_argument(
        "--downstream",
        metavar="STAGE_SELECTOR",
        help=f"Clean the selector and all downstream stages. {selector_help}",
    )
    clean_parser.add_argument("--out", type=Path, metavar="PATH", help=out_help)
    clean_parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation.")

    review_parsers = {}
    for command, help_text in (
        ("reuse", "Keep existing materializations reusable after source changes."),
        ("invalidate", "Mark existing materializations as needing a rerun after source changes."),
    ):
        review_parser = subparsers.add_parser(command, help=help_text)
        review_parser.add_argument(
            "--stage",
            action="append",
            default=[],
            metavar="BASE_STAGE",
            help="Review this base Stage; repeat to take a union. Coordinates are not accepted.",
        )
        review_parser.add_argument("--branch", default="main", metavar="NAME")
        review_parser.add_argument("--out", type=Path, metavar="PATH", help=out_help)
        review_parsers[command] = review_parser

    plan_parser = subparsers.add_parser(
        "plan", help="show selected logical stage topology with exact status"
    )
    argmap.add_stage_selection(plan_parser, selector_help, verb="Show")
    plan_parser.add_argument("--branch", default="main", metavar="NAME")
    plan_parser.add_argument("--out", type=Path, metavar="PATH", help=out_help)
    plan_parser.add_argument(
        "--rehash", action="store_true", help="Ignore persisted stat shortcuts while keying."
    )

    subparsers.add_parser("ls", help="list branch-independent pipeline structure")
    config_parsers = {
        "run": run_parser,
        "status": status_parser,
        "plan": plan_parser,
        "clean": clean_parser,
        **review_parsers,
    }

    if selected_command in config_parsers and selected_command_index is not None:
        command_args = raw_argv[selected_command_index + 1 :]
        if _has_unknown_option_before_config_registration(
            parser=config_parsers[selected_command],
            command_args=command_args,
            args_type=pipeline.Args,
        ):
            parser.error("unknown option or missing option value")

    if selected_command in config_parsers:
        argmap.register_args(config_parsers[selected_command], pipeline.Args)

    namespace = parser.parse_args(raw_argv)
    configure_cli_logging(namespace.verbose, quiet=namespace.command != "run")

    if namespace.command == "ls":
        render_structure(make_console(), pipeline)
        return 0

    resolved = resolve_branch(
        pipeline,
        branch=namespace.branch,
        override_json=namespace.override if namespace.command == "run" else None,
        cli_out=namespace.out,
        allow_bare_output_root=namespace.command == "clean",
    )
    graph = build_graph(pipeline, resolved.axes)
    if namespace.command == "run" and namespace.slice and not resolved.is_temporary:
        raise ValueError("--slice is only allowed on temporary branches")
    args = argmap.model_from_namespace(namespace, pipeline.Args)
    context = resolved_command_context(pipeline, resolved, args, graph=graph)
    try:
        return dispatch_command(
            context,
            namespace,
            confirm=default_confirm,
            review_targets=(
                tuple(namespace.stage) if namespace.command in {"reuse", "invalidate"} else ()
            ),
        )
    except ValueError as error:
        if namespace.command in {"reuse", "invalidate", "status"}:
            parser.error(str(error))
        raise
