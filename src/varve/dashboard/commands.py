"""Shared-backend orchestration for the top-level varve command."""

from __future__ import annotations

import logging
import sys
from contextlib import nullcontext
from pathlib import Path

from rich.console import Console

from varve.cli.commands import execute_review, render_plan
from varve.cli.commands import execute_run as _run_context
from varve.cli.review import (
    BulkReviewEntry,
    BulkReviewFailure,
    render_bulk_source_review,
)
from varve.cli.structure import render_structure
from varve.dashboard.discovery import discover_pipelines, filter_entries
from varve.dashboard.models import PipelineEntry, PipelineState
from varve.dashboard.render import render_bulk_run, render_no_status_matches, render_overview
from varve.dashboard.state import (
    import_entry_pipeline,
    load_state,
    resolve_entry_context,
    resolve_entry_target,
    resolve_structure_pipeline,
)
from varve.engine.review import ReviewAction
from varve.engine.runner import _KeyingSession
from varve.engine.state import EffectiveStatus
from varve.keying.fingerprint import FingerprintSession
from varve.log import configure_cli_logging
from varve.pipeline import Pipeline
from varve.style import BULK_RUN_MARKER, make_console


def discover_scope(
    root: Path,
    *,
    prefix: str | None,
    branch: str | None,
    include_temp: bool,
) -> list[PipelineEntry]:
    entries = discover_pipelines(root, include_temporary=include_temp)
    return filter_entries(
        entries,
        prefix=prefix,
        branch=branch,
        include_temporary=include_temp,
    )


def render_structure_command(
    entries: list[PipelineEntry],
    module: str,
    *,
    console: Console | None = None,
) -> int:
    pipeline, _ = resolve_structure_pipeline(entries, module)
    render_structure(console or make_console(), pipeline)
    return 0


def plan_command(
    entry: PipelineEntry,
    pipeline: type[Pipeline],
    *,
    upto: str | None,
    downstream: str | None,
    only: str | None,
) -> int:
    _, graph = resolve_entry_target(entry, pipeline)
    return render_plan(graph, upto=upto, downstream=downstream, only=only)


def overview_command(
    root: Path,
    *,
    prefix: str | None,
    branch: str | None,
    include_temp: bool,
    rehash: bool,
    statuses: tuple[EffectiveStatus, ...],
    console: Console | None = None,
) -> int:
    console = console or make_console()
    with _loading(console, "Discovering pipelines…") as loading:
        entries = discover_scope(
            root,
            prefix=prefix,
            branch=branch,
            include_temp=include_temp,
        )
        if not entries:
            _print_empty_scope(root, prefix, branch, include_temp)
            return 1
        session = _KeyingSession(fingerprints=FingerprintSession(force_rehash=rehash))
        states = []
        for index, entry in enumerate(entries, start=1):
            if loading is not None:
                loading.update(f"Evaluating pipeline state {index}/{len(entries)}…")
            states.append(load_state(entry, session))
    filtered = [state for state in states if not statuses or state.status in statuses]
    if not filtered:
        render_no_status_matches(console)
        return 0
    render_overview(filtered, console=console)
    return 0


def bulk_review_command(
    root: Path,
    *,
    prefix: str | None,
    branch: str | None,
    include_temp: bool,
    decision: ReviewAction,
    console: Console | None = None,
) -> int:
    console = console or make_console()
    entries = discover_scope(root, prefix=prefix, branch=branch, include_temp=include_temp)
    if not entries:
        _print_empty_scope(root, prefix, branch, include_temp)
        return 1
    session = _KeyingSession()
    results: list[BulkReviewEntry] = []
    failures: list[BulkReviewFailure] = []
    for entry in entries:
        module = entry.module or entry.pipeline_id
        try:
            pipeline = import_entry_pipeline(entry)
            context = resolve_entry_context(entry, pipeline, pipeline.Args())
            result = execute_review(
                context,
                decision=decision,
                _keying_session=session,
            )
            results.append(BulkReviewEntry(module, entry.branch, result))
        except Exception as error:  # noqa: BLE001 - bulk review continues per store.
            failures.append(BulkReviewFailure(module, entry.branch, str(error)))
        finally:
            session.refresh_observations()
    render_bulk_source_review(console, decision, results, failures)
    return 1 if failures else 0


def bulk_run_command(
    root: Path,
    *,
    prefix: str | None,
    branch: str | None,
    include_temp: bool,
    rehash: bool,
    console: Console | None = None,
) -> int:
    console = console or make_console()
    entries = discover_scope(root, prefix=prefix, branch=branch, include_temp=include_temp)
    if not entries:
        _print_empty_scope(root, prefix, branch, include_temp)
        return 1
    session = _KeyingSession(fingerprints=FingerprintSession(force_rehash=rehash))
    final_states: list[PipelineState] = []
    logger = logging.getLogger("varve")
    logging_configured = False
    for index, entry in enumerate(entries, start=1):
        with _loading(console, f"Evaluating pipeline state {index}/{len(entries)}…"):
            state = load_state(entry, session)
        if state.status in {"hit", "needs-review", "error"}:
            final_states.append(state)
            continue
        if not logging_configured:
            configure_cli_logging()
            logging_configured = True
        module = entry.module or entry.pipeline_id
        logger.info("%s run %s --branch %s", BULK_RUN_MARKER, module, entry.branch)
        try:
            pipeline = import_entry_pipeline(entry)
            context = resolve_entry_context(entry, pipeline, pipeline.Args())
            _run_context(context, rehash=rehash)
        except Exception as error:  # noqa: BLE001 - bulk run continues with later entries.
            logger.error("failed to run %s --branch %s: %s", module, entry.branch, error)
        finally:
            session.refresh_observations()
        final_states.append(load_state(entry, session))
        session.refresh_observations()

    render_bulk_run(final_states, console=console)
    incomplete = [state for state in final_states if not state.complete]
    if not incomplete:
        return 0
    return 2 if all(state.status == "needs-review" for state in incomplete) else 1


def _print_empty_scope(
    root: Path,
    prefix: str | None,
    branch: str | None,
    include_temp: bool,
) -> None:
    print(
        "No pipelines match discovery scope: "
        f"root={root}, prefix={prefix or '*'}, branch={branch or '*'}, "
        f"temporary={'included' if include_temp else 'excluded'}",
        file=sys.stderr,
    )


def _loading(console: Console, message: str):
    return console.status(message, spinner="dots") if console.is_terminal else nullcontext(None)
