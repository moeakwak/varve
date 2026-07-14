"""Top-level varve CLI over discovered pipeline stores."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from varve.cli import argmap
from varve.cli.clean import default_confirm
from varve.cli.commands import dispatch_command
from varve.cli.structure import render_structure
from varve.dashboard.commands import (
    DiscoveryScope,
    bulk_review_command,
    bulk_run_command,
    overview_command,
)
from varve.dashboard.discovery import discover_pipelines
from varve.dashboard.models import PipelineEntry
from varve.dashboard.state import (
    import_entry_pipeline,
    resolve_entry_context,
    resolve_module_entry,
    resolve_structure_pipeline,
)
from varve.log import configure_cli_logging
from varve.pipeline import Pipeline
from varve.style import make_console

_DYNAMIC_COMMANDS = {"run", "status", "plan", "clean", "reuse", "invalidate"}
_COMMANDS = _DYNAMIC_COMMANDS | {"ls"}


def _parse_to_exit(argv: list[str]) -> None:
    _parser().parse_args(argv)
    raise AssertionError("argparse did not exit")


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]
    if not raw_argv:
        raw_argv = ["ls"]
    if raw_argv in (["--help"], ["-h"]):
        _parse_to_exit(raw_argv)
    command = raw_argv[0]
    if command not in _COMMANDS:
        _parse_to_exit(raw_argv)

    if any(token.split("=", 1)[0] in {"--out", "--override", "--slice"} for token in raw_argv):
        _parse_to_exit(raw_argv)

    pipeline: type[Pipeline] | None = None
    entry: PipelineEntry | None = None
    module: str | None = None
    if command in _DYNAMIC_COMMANDS:
        target = raw_argv[1] if len(raw_argv) > 1 else None
        if target in {"-h", "--help"}:
            _parse_to_exit(raw_argv)
        if target == "--all" and command in {"run", "reuse", "invalidate"}:
            scope = _dynamic_scope(command, raw_argv[1:])
        else:
            if target is None or target.startswith("-"):
                _missing_target_error(command)
            module = target
            if command in {"run", "reuse", "invalidate"} and "--all" in raw_argv[2:]:
                parser = _parser()
                parser.error(f"varve {command} requires exactly one of MODULE or --all")
            scope = _dynamic_scope(command, raw_argv[2:])
            entries = discover_pipelines(scope.root, include_temporary=scope.include_temp)

    if module is not None:
        try:
            entry = resolve_module_entry(entries, module, branch=scope.branch or "main")
            pipeline = import_entry_pipeline(entry)
        except Exception as error:  # noqa: BLE001 - target resolution is a command failure.
            print(str(error), file=sys.stderr)
            return 1

    parser = _parser(pipeline=pipeline, dynamic_command=command)
    namespace = parser.parse_args(raw_argv)
    _validate_surface(parser, namespace)
    if module is not None and namespace.module != module:
        parser.error("parsed MODULE does not match the resolved target")

    try:
        if namespace.command == "ls":
            if namespace.module is None:
                return overview_command(
                    DiscoveryScope(
                        namespace.root,
                        namespace.prefix,
                        namespace.branch,
                        namespace.include_temp,
                    ),
                    rehash=namespace.rehash,
                    statuses=tuple(namespace.status),
                )
            entries = discover_pipelines(
                namespace.root,
                include_temporary=namespace.include_temp,
            )
            structure = resolve_structure_pipeline(entries, namespace.module)
            render_structure(make_console(), structure)
            return 0

        all_targets = getattr(namespace, "all", False)
        assert namespace.module is not None or all_targets
        if all_targets:
            command_scope = DiscoveryScope(
                namespace.root, namespace.prefix, namespace.branch, namespace.include_temp
            )
            if namespace.command == "run":
                return bulk_run_command(command_scope, rehash=namespace.rehash)
            return bulk_review_command(command_scope, decision=namespace.command)

        assert entry is not None and pipeline is not None
        pipeline_args = argmap.model_from_namespace(namespace, pipeline.Args)
        context = resolve_entry_context(entry, pipeline, pipeline_args)
        if namespace.command == "run":
            configure_cli_logging()
        return dispatch_command(
            context,
            namespace,
            confirm=default_confirm,
            review_targets=(
                tuple(namespace.stage) if namespace.command in {"reuse", "invalidate"} else ()
            ),
            target_module=entry.module,
        )
    except Exception as error:  # noqa: BLE001 - CLI reports backend diagnostics as exit 1.
        print(str(error), file=sys.stderr)
        return 1
    parser.error(f"Unknown command: {namespace.command}")


def _parser(
    *,
    add_help: bool = True,
    pipeline: type[Pipeline] | None = None,
    dynamic_command: str | None = None,
) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="varve", add_help=add_help)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        return subparsers.add_parser(name, help=help_text, add_help=add_help)

    ls_parser = command("ls", "show exact pipeline overview or one pipeline structure")
    ls_parser.add_argument("module", nargs="?", metavar="MODULE")
    ls_parser.add_argument("--root", type=Path, default=Path.cwd())
    ls_parser.add_argument("--prefix", metavar="MODULE_PREFIX")
    ls_parser.add_argument("--branch", metavar="NAME")
    ls_parser.add_argument("--include-temp", action="store_true")
    ls_parser.add_argument("--rehash", action="store_true")
    ls_parser.add_argument(
        "--status",
        action="append",
        default=[],
        choices=("hit", "needs-review", "needs-run", "resume", "failed", "error"),
        metavar="STATUS",
    )

    dynamic_help = {
        "status": "show exact status for one pipeline store",
        "run": "run one pipeline store or every filtered store",
        "plan": "show selected logical stage topology for one pipeline store",
        "clean": "clean one pipeline store",
    }
    for name in ("status", "run", "reuse", "invalidate", "plan", "clean"):
        help_text = dynamic_help.get(name, f"{name} source changes for one or all pipeline stores")
        dynamic = command(name, help_text)
        target = (
            "MODULE [OPTIONS]"
            if name in {"status", "plan", "clean"}
            else "(MODULE [OPTIONS] | --all [OPTIONS])"
        )
        dynamic.usage = f"varve {name} {target}"
        _add_dynamic_options(dynamic, name, positional=True, help_text=argmap.STAGE_SELECTOR_HELP)
        if pipeline is not None and dynamic_command == name:
            argmap.register_args(dynamic, pipeline.Args)

    return parser


def _dynamic_scope(
    command: str,
    argv: list[str],
) -> argparse.Namespace:
    """Parse discovery and static options without assigning a positional MODULE."""

    parser = argparse.ArgumentParser(add_help=False)
    _add_dynamic_options(parser, command, positional=False)
    return parser.parse_known_args(argv)[0]


def _add_dynamic_options(
    parser: argparse.ArgumentParser,
    command: str,
    *,
    positional: bool,
    help_text: str | None = None,
) -> None:
    if positional:
        parser.add_argument("module", nargs="?", metavar="MODULE")
    if command in {"run", "reuse", "invalidate"}:
        parser.add_argument("--all", action="store_true")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--branch", metavar="NAME")
    parser.add_argument("--include-temp", action="store_true")
    if command in {"run", "reuse", "invalidate"}:
        parser.add_argument("--prefix", metavar="MODULE_PREFIX")
    if command == "run":
        argmap.add_stage_selection(parser, help_text)
        parser.add_argument("--force", "-f", action="store_true")
        parser.add_argument("--rehash", action="store_true")
        display = parser.add_mutually_exclusive_group()
        display.add_argument("--expand", action="store_true")
        display.add_argument("--compact", action="store_true")
    elif command == "status":
        parser.add_argument("--stage", metavar="STAGE_SELECTOR", help=help_text)
        parser.add_argument("--expand", action="store_true")
        parser.add_argument("--rehash", action="store_true")
    elif command == "plan":
        argmap.add_stage_selection(parser, help_text)
        parser.add_argument("--rehash", action="store_true")
    elif command in {"reuse", "invalidate"}:
        argmap.add_review_selection(parser)
    elif command == "clean":
        parser.add_argument("--downstream", metavar="STAGE_SELECTOR", help=help_text)
        parser.add_argument("--yes", "-y", action="store_true")


def _missing_target_error(command: str) -> None:
    message = {
        "status": "varve status requires MODULE; use 'varve ls' for the overview",
        "clean": "varve clean requires MODULE",
    }.get(command, f"varve {command} requires exactly one of MODULE or --all")
    _parser().error(message)


def _validate_surface(parser: argparse.ArgumentParser, namespace: argparse.Namespace) -> None:
    command = namespace.command
    module = getattr(namespace, "module", None)
    all_targets = getattr(namespace, "all", False)
    if command in {"run", "reuse", "invalidate"}:
        if bool(module) == bool(all_targets):
            parser.error(f"varve {command} requires exactly one of MODULE or --all")
        if module is not None and namespace.prefix is not None:
            parser.error("--prefix is only available with --all")
    if command == "ls" and module is not None:
        if namespace.prefix is not None or namespace.branch is not None or namespace.status:
            parser.error("varve ls MODULE accepts only --root and --include-temp")
        if namespace.rehash:
            parser.error("varve ls MODULE does not evaluate store state")
    if command == "run" and all_targets:
        if any((namespace.upto, namespace.downstream, namespace.only)):
            parser.error("varve run --all does not accept stage selection")
        if namespace.force or namespace.expand or namespace.compact:
            parser.error("varve run --all does not accept --force or display selection")
    if command in {"reuse", "invalidate"} and all_targets and namespace.stage:
        parser.error(f"varve {command} --all does not accept stage selection")
