"""Shared-backend orchestration for the top-level varve command."""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import NamedTuple

from rich.console import Console

from varve.cli.commands import execute_review
from varve.cli.commands import execute_run as _run_context
from varve.cli.review import (
    BulkReviewEntry,
    BulkReviewFailure,
    render_bulk_source_review,
)
from varve.dashboard.discovery import discover_pipelines, filter_entries
from varve.dashboard.models import PipelineEntry, PipelineState
from varve.dashboard.render import render_bulk_run, render_no_status_matches, render_overview
from varve.dashboard.state import (
    import_entry_pipeline,
    load_state,
    resolve_entry_context,
)
from varve.engine.review import ReviewAction
from varve.engine.runner import _KeyingSession
from varve.engine.state import EffectiveStatus
from varve.keying.fingerprint import FingerprintSession
from varve.log import configure_cli_logging
from varve.style import BULK_RUN_MARKER, make_console
from varve.style import loading as _loading


class DiscoveryScope(NamedTuple):
    root: Path
    prefix: str | None
    branch: str | None
    include_temp: bool


def discover_scope(scope: DiscoveryScope) -> list[PipelineEntry]:
    entries = discover_pipelines(scope.root, include_temporary=scope.include_temp)
    return filter_entries(
        entries,
        prefix=scope.prefix,
        branch=scope.branch,
        include_temporary=scope.include_temp,
    )


def overview_command(
    scope: DiscoveryScope,
    *,
    rehash: bool,
    statuses: tuple[EffectiveStatus, ...],
    console: Console | None = None,
) -> int:
    console = console or make_console()
    with _loading(console, "Discovering pipelines…") as loading:
        entries = discover_scope(scope)
        if not entries:
            _print_empty_scope(scope)
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
    scope: DiscoveryScope,
    *,
    decision: ReviewAction,
    console: Console | None = None,
) -> int:
    console = console or make_console()
    entries = discover_scope(scope)
    if not entries:
        _print_empty_scope(scope)
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
    scope: DiscoveryScope,
    *,
    rehash: bool,
    console: Console | None = None,
) -> int:
    console = console or make_console()
    entries = discover_scope(scope)
    if not entries:
        _print_empty_scope(scope)
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


def _print_empty_scope(scope: DiscoveryScope) -> None:
    print(
        "No pipelines match discovery scope: "
        f"root={scope.root}, prefix={scope.prefix or '*'}, branch={scope.branch or '*'}, "
        f"temporary={'included' if scope.include_temp else 'excluded'}",
        file=sys.stderr,
    )
