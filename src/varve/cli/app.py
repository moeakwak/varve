"""Command-line interface for Pipeline subclasses."""

from __future__ import annotations

import argparse
import re
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

from pydantic import BaseModel

from varve.branch_config import resolve_branch
from varve.cli import argmap
from varve.cli.clean import clean
from varve.cli.review import render_source_review
from varve.cli.run import render_run_outcomes
from varve.cli.status import render_status, status_view
from varve.cli.structure import render_structure
from varve.command import resolved_command_context
from varve.engine.review import ReviewAction
from varve.engine.runner import ReviewRequiredError, record_source_review, run, selected_stages
from varve.log import configure_cli_logging
from varve.matrix import build_graph
from varve.pipeline import Pipeline
from varve.status import collect_pipeline_status
from varve.style import make_console

_CONFIG_COMMANDS = {"run", "status", "clean", "accept", "reject"}
_NEGATIVE_NUMBER_RE = re.compile(r"^-\d+$|^-\d*\.\d+$")
_COMMAND_OPTION_ARITIES = {
    "run": {
        "--branch": 1,
        "--override": 1,
        "--upto": 1,
        "--downstream": 1,
        "--only": 1,
        "--slice": 1,
        "--force": 0,
        "--rehash": 0,
        "-f": 0,
        "--expand": 0,
        "--compact": 0,
        "--out": 1,
    },
    "status": {
        "--branch": 1,
        "--out": 1,
        "--expand": 0,
        "--rehash": 0,
    },
    "clean": {
        "--branch": 1,
        "--downstream": 1,
        "--out": 1,
        "--yes": 0,
        "-y": 0,
    },
    "accept": {"--branch": 1, "--out": 1, "--stage": 1},
    "reject": {"--branch": 1, "--out": 1, "--stage": 1},
    "plan": {"--upto": 1, "--downstream": 1, "--only": 1, "--branch": 1, "--out": 1},
}


def _args_from_namespace(
    pipeline: type[Pipeline],
    namespace: argparse.Namespace,
) -> BaseModel:
    init_kwargs = argmap.collect_cli_args_namespace(namespace, pipeline.Args)
    return pipeline.Args.model_validate(init_kwargs)


def _print_plan(
    graph,
    *,
    upto: str | None,
    downstream: str | None,
    only: str | None,
) -> None:
    selected = selected_stages(graph, upto=upto, downstream=downstream, only=only)
    print(" -> ".join(name for name in graph.topo_order() if name in selected))


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
    selector_help = (
        "Base selects all active cells; omitted axes are wildcards; "
        "full coordinates select one cell."
    )
    run_stage.add_argument(
        "--upto",
        metavar="STAGE_SELECTOR",
        help=f"Run the selector and all upstream stages. {selector_help}",
    )
    run_stage.add_argument(
        "--downstream",
        metavar="STAGE_SELECTOR",
        help=f"Run the selector and all downstream stages. {selector_help}",
    )
    run_stage.add_argument(
        "--only", metavar="STAGE_SELECTOR", help=f"Run only the selector. {selector_help}"
    )
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
        ("accept", "Mark source changes as not requiring a rerun."),
        ("reject", "Mark source changes as requiring a rerun."),
    ):
        review_parser = subparsers.add_parser(command, help=help_text)
        review_parser.add_argument(
            "--stage",
            action="append",
            default=[],
            metavar="STAGE_SELECTOR",
            help=f"Review this selector; repeat to take a union. {selector_help}",
        )
        review_parser.add_argument("--branch", default="main", metavar="NAME")
        review_parser.add_argument("--out", type=Path, metavar="PATH", help=out_help)
        review_parsers[command] = review_parser

    plan_parser = subparsers.add_parser("plan", help="print selected stage order")
    plan_stage = plan_parser.add_mutually_exclusive_group()
    plan_stage.add_argument(
        "--upto",
        metavar="STAGE_SELECTOR",
        help=f"Print the selector and all upstream stages. {selector_help}",
    )
    plan_stage.add_argument(
        "--downstream",
        metavar="STAGE_SELECTOR",
        help=f"Print the selector and all downstream stages. {selector_help}",
    )
    plan_stage.add_argument(
        "--only", metavar="STAGE_SELECTOR", help=f"Print only the selector. {selector_help}"
    )
    plan_parser.add_argument("--branch", default="main", metavar="NAME")
    plan_parser.add_argument("--out", type=Path, metavar="PATH")

    subparsers.add_parser("ls", help="list branch-independent pipeline structure")

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
    if selected_command == "clean":
        argmap.register_args(clean_parser, pipeline.Args)
    if selected_command in review_parsers:
        argmap.register_args(review_parsers[selected_command], pipeline.Args)

    namespace = parser.parse_args(raw_argv)
    configure_cli_logging(namespace.verbose, quiet=namespace.command != "run")

    if namespace.command == "ls":
        render_structure(make_console(), pipeline)
        return 0
    if namespace.command == "plan":
        resolved = resolve_branch(
            pipeline, branch=namespace.branch, override_json=None, cli_out=namespace.out
        )
        _print_plan(
            build_graph(pipeline, resolved.axes),
            upto=namespace.upto,
            downstream=namespace.downstream,
            only=namespace.only,
        )
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
    args = _args_from_namespace(pipeline, namespace)
    context = resolved_command_context(pipeline, resolved, args, graph=graph)
    if namespace.command in {"accept", "reject"}:
        decision: ReviewAction = "accept" if namespace.command == "accept" else "reject"
        try:
            result = record_source_review(
                context.pipeline,
                context.config,
                decision=decision,
                args=context.args,
                targets=tuple(namespace.stage),
                cli_out=context.output_base,
                branch=context.branch,
                is_temporary=context.is_temporary,
                axes=context.axes,
                graph=context.graph,
            )
        except ValueError as error:
            parser.error(str(error))
        render_source_review(make_console(), result)
    elif namespace.command == "status":
        console = make_console()
        loading = (
            console.status("Evaluating pipeline status…", spinner="dots")
            if console.is_terminal
            else nullcontext()
        )
        with loading:
            try:
                status = collect_pipeline_status(
                    context,
                    selector=namespace.stage,
                    rehash=namespace.rehash,
                )
            except ValueError as error:
                parser.error(str(error))
        render_status(
            console,
            status,
            view=status_view(status, expand=namespace.expand),
            dependency_depth=0,
        )
    elif namespace.command == "clean":
        allowed_roots = (
            None
            if isinstance(context.config, SimpleNamespace)
            else context.pipeline.clean_roots(context.config)
        )
        clean(
            context.pipeline,
            context.config,
            cli_out=context.output_base,
            branch=context.branch,
            is_temporary=context.is_temporary,
            target=namespace.downstream,
            yes=namespace.yes,
            allowed_roots=allowed_roots,
            confirm=_default_confirm,
            axes=context.axes,
            graph=context.graph,
        )
    elif namespace.command == "run":
        display_mode = "expand" if namespace.expand else "compact" if namespace.compact else "auto"
        try:
            outcomes = run(
                context.pipeline,
                context.config,
                args=context.args,
                upto=namespace.upto,
                downstream=namespace.downstream,
                force=namespace.force,
                cli_out=context.output_base,
                branch=context.branch,
                is_temporary=context.is_temporary,
                temporary_config=context.resolved.temporary_config,
                axes=context.axes,
                temporary_axes=context.resolved.temporary_axes,
                only=namespace.only,
                slices=tuple(namespace.slice),
                graph=context.graph,
                display_mode=display_mode,
                rehash=namespace.rehash,
            )
        except ReviewRequiredError as error:
            print(str(error), file=sys.stderr)
            return 2
        render_run_outcomes(make_console(), outcomes, elapsed=True)
    return 0
