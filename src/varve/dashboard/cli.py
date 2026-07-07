"""Top-level varve dashboard CLI."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from varve.dashboard.discovery import discover_pipelines
from varve.dashboard.models import PipelineEntry
from varve.dashboard.render import render_detail, render_overview
from varve.dashboard.state import import_entry_pipeline, load_state, resolve_entry_branch
from varve.engine.runner import run
from varve.log import configure_cli_logging
from varve.style import REFRESH_MARKER

_EXECUTABLE_STATUSES = {"artifact-missing", "dirty", "no-cache", "resume", "stale"}


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "show":
        return _show(args.root, args.pipeline, args.branch, args.include_temp)
    if args.command == "refresh":
        return _refresh(args.root, args.include_temp, args.prefix)
    root = args.root if args.command == "ls" else Path.cwd()
    return _ls(root, getattr(args, "include_temp", False))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="varve")
    subparsers = parser.add_subparsers(dest="command")

    ls_parser = subparsers.add_parser("ls", help="list discovered pipeline stores")
    ls_parser.add_argument("--root", type=Path, default=Path.cwd())
    _add_include_temp(ls_parser)

    show_parser = subparsers.add_parser("show", help="show one pipeline store")
    show_parser.add_argument("pipeline")
    show_parser.add_argument("--root", type=Path, default=Path.cwd())
    show_parser.add_argument("--branch", default="main")
    _add_include_temp(show_parser)

    refresh_parser = subparsers.add_parser("refresh", help="run executable discovered pipelines")
    refresh_parser.add_argument("--root", type=Path, default=Path.cwd())
    refresh_parser.add_argument(
        "--prefix",
        help="only refresh pipelines whose module starts with this prefix",
    )
    _add_include_temp(refresh_parser)
    return parser


def _add_include_temp(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--include-temp",
        action="store_true",
        help="include temporary override branches under out/.tmp",
    )


def _ls(root: Path, include_temp: bool) -> int:
    entries = discover_pipelines(root, include_temporary=include_temp)
    if not entries:
        print(f"No pipelines found under {root}", file=sys.stderr)
        return 1
    states = [load_state(entry) for entry in entries]
    render_overview(states)
    return 0


def _show(root: Path, pipeline_id: str, branch: str, include_temp: bool) -> int:
    entries = discover_pipelines(root, include_temporary=include_temp)
    by_key = {(entry.pipeline_id, entry.branch): entry for entry in entries}
    entry = by_key.get((pipeline_id, branch))
    if entry is None:
        print(f"Unknown pipeline: {pipeline_id} (branch {branch})", file=sys.stderr)
        if by_key:
            print("Available pipelines:", file=sys.stderr)
            for known_id, known_branch in sorted(by_key):
                print(f"  {known_id} --branch {known_branch}", file=sys.stderr)
        else:
            print(f"No pipelines found under {root}", file=sys.stderr)
        return 1
    render_detail(load_state(entry))
    return 0


def _refresh(root: Path, include_temp: bool, prefix: str | None = None) -> int:
    entries = discover_pipelines(root, include_temporary=include_temp)
    if prefix is not None:
        entries = [
            entry
            for entry in entries
            if entry.module is not None and entry.module.startswith(prefix)
        ]
    if not entries:
        print(f"No pipelines found under {root}", file=sys.stderr)
        return 1

    refreshed = 0
    failed = 0
    logger = logging.getLogger("varve")
    logging_configured = False
    for entry in entries:
        state = load_state(entry)
        if state.status not in _EXECUTABLE_STATUSES:
            continue
        if not logging_configured:
            configure_cli_logging()
            logging_configured = True
        # Route the per-pipeline header through the varve logger so it shares the
        # timestamp column and styling with the stage lines that _run_entry emits.
        # The leading marker lets the highlighter accent the whole header line.
        logger.info("%s refresh %s --branch %s", REFRESH_MARKER, entry.pipeline_id, entry.branch)
        try:
            _run_entry(entry)
        except Exception as error:  # noqa: BLE001 - refresh should continue with later stores.
            failed += 1
            logger.error(
                "failed to refresh %s --branch %s: %s",
                entry.pipeline_id,
                entry.branch,
                error,
            )
        else:
            refreshed += 1

    if refreshed == 0 and failed == 0:
        print("No executable pipelines found")
    return 1 if failed else 0


def _run_entry(entry: PipelineEntry) -> None:
    pipeline = import_entry_pipeline(entry)
    resolved = resolve_entry_branch(entry, pipeline)
    run(
        pipeline,
        resolved.config,
        args=pipeline.Args(),
        cli_out=resolved.output_base,
        branch=resolved.branch,
        is_temporary=resolved.is_temporary,
        temporary_config=resolved.temporary_config,
    )
