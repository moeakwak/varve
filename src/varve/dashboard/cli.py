"""Top-level varve dashboard CLI."""

from __future__ import annotations

import argparse
import logging
import sys
from contextlib import nullcontext
from pathlib import Path

from varve.dashboard.discovery import discover_pipelines
from varve.dashboard.models import PipelineEntry
from varve.dashboard.render import render_detail, render_overview
from varve.dashboard.state import import_entry_pipeline, load_state, resolve_entry_branch
from varve.engine.runner import _KeyingSession, record_source_review, run
from varve.keying.fingerprint import FingerprintSession
from varve.log import configure_cli_logging
from varve.matrix import build_graph
from varve.style import REFRESH_MARKER, make_console

_EXECUTABLE_STATUSES = {"needs-run", "resume", "failed"}


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "show":
        return _show(args.root, args.pipeline, args.branch, args.include_temp, args.rehash)
    if args.command == "refresh":
        return _refresh(args.root, args.include_temp, args.prefix, args.rehash)
    if args.command in {"accept", "reject"}:
        return _review(args.root, args.pipeline, args.branch, tuple(args.stages), args.command)
    root = args.root if args.command == "ls" else Path.cwd()
    return _ls(root, getattr(args, "include_temp", False), getattr(args, "rehash", False))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="varve")
    subparsers = parser.add_subparsers(dest="command")

    ls_parser = subparsers.add_parser("ls", help="list discovered pipeline stores")
    ls_parser.add_argument("--root", type=Path, default=Path.cwd())
    _add_include_temp(ls_parser)
    _add_rehash(ls_parser)

    show_parser = subparsers.add_parser("show", help="show one pipeline store")
    show_parser.add_argument("pipeline")
    show_parser.add_argument("--root", type=Path, default=Path.cwd())
    show_parser.add_argument("--branch", default="main")
    _add_include_temp(show_parser)
    _add_rehash(show_parser)

    refresh_parser = subparsers.add_parser("refresh", help="run executable discovered pipelines")
    refresh_parser.add_argument("--root", type=Path, default=Path.cwd())
    refresh_parser.add_argument(
        "--prefix",
        help="only refresh pipelines whose module starts with this prefix",
    )
    _add_include_temp(refresh_parser)
    _add_rehash(refresh_parser)
    for command, help_text in (
        ("accept", "mark source changes as not requiring a rerun"),
        ("reject", "mark source changes as requiring a rerun"),
    ):
        review_parser = subparsers.add_parser(command, help=help_text)
        review_parser.add_argument("pipeline")
        review_parser.add_argument("stages", nargs="*", metavar="STAGE")
        review_parser.add_argument("--root", type=Path, default=Path.cwd())
        review_parser.add_argument("--branch", default="main")
    return parser


def _add_include_temp(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--include-temp",
        action="store_true",
        help="include temporary override branches under out/.tmp",
    )


def _add_rehash(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--rehash",
        action="store_true",
        help="ignore persisted stat shortcuts while evaluating fingerprints",
    )


def _ls(root: Path, include_temp: bool, rehash: bool = False) -> int:
    console = make_console()
    with _loading(console, "Discovering pipelines…") as loading:
        entries = discover_pipelines(root, include_temporary=include_temp)
        session = _KeyingSession(fingerprints=FingerprintSession(force_rehash=rehash))
        states = []
        for index, entry in enumerate(entries, start=1):
            if loading is not None:
                loading.update(f"Evaluating pipeline state {index}/{len(entries)}…")
            states.append(load_state(entry, session))
    if not entries:
        print(f"No pipelines found under {root}", file=sys.stderr)
        return 1
    render_overview(states)
    return 0


def _show(
    root: Path,
    pipeline_id: str,
    branch: str,
    include_temp: bool,
    rehash: bool = False,
) -> int:
    console = make_console()
    with _loading(console, "Discovering pipelines…") as loading:
        entries = discover_pipelines(root, include_temporary=include_temp)
        by_key = {(entry.pipeline_id, entry.branch): entry for entry in entries}
        entry = by_key.get((pipeline_id, branch))
        if entry is not None:
            if loading is not None:
                loading.update("Evaluating pipeline state 1/1…")
            state = load_state(
                entry,
                _KeyingSession(fingerprints=FingerprintSession(force_rehash=rehash)),
            )
    if entry is None:
        print(f"Unknown pipeline: {pipeline_id} (branch {branch})", file=sys.stderr)
        if by_key:
            print("Available pipelines:", file=sys.stderr)
            for known_id, known_branch in sorted(by_key):
                print(f"  {known_id} --branch {known_branch}", file=sys.stderr)
        else:
            print(f"No pipelines found under {root}", file=sys.stderr)
        return 1
    render_detail(state)
    return 0


def _review(
    root: Path,
    pipeline_id: str,
    branch: str,
    stages: tuple[str, ...],
    decision: str,
) -> int:
    entries = discover_pipelines(root, include_temporary=True)
    entry = next(
        (item for item in entries if item.pipeline_id == pipeline_id and item.branch == branch),
        None,
    )
    if entry is None:
        print(f"Unknown pipeline: {pipeline_id} (branch {branch})", file=sys.stderr)
        return 1
    pipeline = import_entry_pipeline(entry)
    resolved = resolve_entry_branch(entry, pipeline)
    graph = build_graph(pipeline, resolved.axes)
    changed = record_source_review(
        pipeline,
        resolved.config,
        decision=decision,
        args=pipeline.Args(),
        targets=stages,
        cli_out=resolved.output_base,
        branch=resolved.branch,
        is_temporary=resolved.is_temporary,
        axes=resolved.axes,
        graph=graph,
    )
    print(f"{decision.title()}ed source changes for {len(changed)} stage(s).")
    return 0


def _refresh(
    root: Path,
    include_temp: bool,
    prefix: str | None = None,
    rehash: bool = False,
) -> int:
    console = make_console()
    with _loading(console, "Discovering pipelines…"):
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

    logger = logging.getLogger("varve")
    logging_configured = False
    session = _KeyingSession(fingerprints=FingerprintSession(force_rehash=rehash))
    final_states = []
    for index, entry in enumerate(entries, start=1):
        with _loading(console, f"Evaluating pipeline state {index}/{len(entries)}…"):
            state = load_state(entry, session)
        if state.error is not None or state.pending_reviews:
            final_states.append(state)
            continue
        if state.status not in _EXECUTABLE_STATUSES:
            final_states.append(state)
            continue
        if not logging_configured:
            configure_cli_logging()
            logging_configured = True
        # Route the per-pipeline header through the varve logger so it shares the
        # timestamp column and styling with the stage lines that _run_entry emits.
        # The leading marker lets the highlighter accent the whole header line.
        logger.info("%s refresh %s --branch %s", REFRESH_MARKER, entry.pipeline_id, entry.branch)
        try:
            if rehash:
                _run_entry(entry, rehash=True)
            else:
                _run_entry(entry)
        except Exception as error:  # noqa: BLE001 - refresh should continue with later stores.
            logger.error(
                "failed to refresh %s --branch %s: %s",
                entry.pipeline_id,
                entry.branch,
                error,
            )
        finally:
            session.refresh_observations()
        final_states.append(load_state(entry, session))
        session.refresh_observations()

    incomplete = [state for state in final_states if not state.complete]
    if not incomplete:
        print("All selected pipelines are complete.")
        return 0

    print("Refresh incomplete")
    reviews = [state for state in incomplete if state.pending_reviews]
    failures = [
        (state, stage) for state in incomplete for stage in state.stages if stage.status == "failed"
    ]
    errors = [state for state in incomplete if state.error is not None]
    stage_errors = [
        (state, stage) for state in incomplete for stage in state.stages if stage.status == "error"
    ]
    pending = [
        (state, stage)
        for state in incomplete
        for stage in state.stages
        if stage.status in {"needs-run", "resume"}
    ]
    if reviews:
        print("\nREVIEW REQUIRED")
        for state in reviews:
            for stage in state.stages:
                if stage.source_review == "pending":
                    print(f"{state.entry.pipeline_id}  {state.entry.branch}  {stage.name}")
    if failures:
        print("\nFAILED")
        for state, stage in failures:
            print(
                f"{state.entry.pipeline_id}  {state.entry.branch}  {stage.name}  "
                f"{stage.failure or stage.reason}"
            )
    if errors or stage_errors:
        print("\nERROR")
        for state in errors:
            assert state.error is not None
            print(
                f"{state.entry.pipeline_id}  {state.entry.branch}  "
                f"{state.error.phase}  {state.error.message}"
            )
        for state, stage in stage_errors:
            print(
                f"{state.entry.pipeline_id}  {state.entry.branch}  "
                f"evaluate  {stage.name}: {stage.reason}"
            )
    if pending:
        print("\nSTILL PENDING")
        for state, stage in pending:
            print(
                f"{state.entry.pipeline_id}  {state.entry.branch}  {stage.name}  "
                f"{stage.status}  {stage.reason}"
            )
    only_reviews = (
        bool(reviews) and not failures and not errors and not stage_errors and not pending
    )
    return 2 if only_reviews else 1


def _run_entry(entry: PipelineEntry, *, rehash: bool = False) -> None:
    pipeline = import_entry_pipeline(entry)
    resolved = resolve_entry_branch(entry, pipeline)
    graph = build_graph(pipeline, resolved.axes)
    run(
        pipeline,
        resolved.config,
        args=pipeline.Args(),
        cli_out=resolved.output_base,
        branch=resolved.branch,
        is_temporary=resolved.is_temporary,
        temporary_config=resolved.temporary_config,
        axes=resolved.axes,
        temporary_axes=resolved.temporary_axes,
        graph=graph,
        rehash=rehash,
    )


def _loading(console, message: str):
    if not console.is_terminal:
        return nullcontext(None)
    return console.status(message, spinner="dots")
