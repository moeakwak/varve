"""Shared-backend orchestration for the top-level varve command."""

from __future__ import annotations

import logging
import sys
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from rich.console import Console

from varve.cli.clean import clean
from varve.cli.review import (
    BulkReviewEntry,
    BulkReviewFailure,
    render_bulk_source_review,
    render_source_review,
)
from varve.cli.run import render_run_outcomes
from varve.cli.status import render_status, status_view
from varve.cli.structure import render_structure
from varve.command import ResolvedCommandContext
from varve.dashboard.discovery import discover_pipelines, filter_entries
from varve.dashboard.models import PipelineEntry, PipelineState
from varve.dashboard.render import render_bulk_run, render_no_status_matches, render_overview
from varve.dashboard.state import (
    load_state,
    resolve_entry_context,
    resolve_entry_target,
    resolve_structure_pipeline,
)
from varve.engine.review import ReviewAction
from varve.engine.run_display import RunDisplayMode
from varve.engine.runner import ReviewRequiredError, _KeyingSession, record_source_review, run
from varve.engine.state import EffectiveStatus
from varve.keying.fingerprint import FingerprintSession
from varve.log import configure_cli_logging
from varve.pipeline import Pipeline
from varve.status import collect_pipeline_status
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


def status_command(
    entry: PipelineEntry,
    pipeline: type[Pipeline],
    pipeline_args: Any,
    *,
    selector: str | None,
    expand: bool,
    rehash: bool,
    console: Console | None = None,
) -> int:
    console = console or make_console()
    context = resolve_entry_context(entry, pipeline, pipeline_args)
    session = _KeyingSession(fingerprints=FingerprintSession(force_rehash=rehash))
    with _loading(console, "Evaluating pipeline status…"):
        status = collect_pipeline_status(
            context,
            selector=selector,
            rehash=rehash,
            session=session,
        )
    render_status(
        console,
        status,
        view=status_view(status, expand=expand),
        target_module=entry.module,
    )
    return 0


def plan_command(
    entry: PipelineEntry,
    pipeline: type[Pipeline],
    *,
    upto: str | None,
    downstream: str | None,
    only: str | None,
) -> int:
    target = resolve_entry_target(entry, pipeline)
    selected = target.graph.selected(upto=upto, downstream=downstream, only=only)
    print(" -> ".join(name for name in target.graph.topo_order() if name in selected))
    return 0


def review_command(
    entry: PipelineEntry,
    pipeline: type[Pipeline],
    pipeline_args: Any,
    *,
    decision: ReviewAction,
    console: Console | None = None,
) -> int:
    context = resolve_entry_context(entry, pipeline, pipeline_args)
    result = record_source_review(
        context.pipeline,
        context.config,
        decision=decision,
        args=context.args,
        cli_out=context.output_base,
        branch=context.branch,
        is_temporary=context.is_temporary,
        axes=context.axes,
        graph=context.graph,
    )
    render_source_review(console or make_console(), result)
    return 0


def clean_command(
    entry: PipelineEntry,
    pipeline: type[Pipeline],
    pipeline_args: Any,
    *,
    downstream: str | None,
    yes: bool,
    confirm,
) -> int:
    context = resolve_entry_context(entry, pipeline, pipeline_args)
    allowed_roots = (
        None
        if isinstance(context.config, SimpleNamespace)
        else pipeline.clean_roots(context.config)
    )
    clean(
        pipeline,
        context.config,
        cli_out=context.output_base,
        branch=context.branch,
        is_temporary=context.is_temporary,
        target=downstream,
        yes=yes,
        allowed_roots=allowed_roots,
        confirm=confirm,
        axes=context.axes,
        graph=context.graph,
    )
    return 0


def run_command(
    entry: PipelineEntry,
    pipeline: type[Pipeline],
    pipeline_args: Any,
    *,
    upto: str | None,
    downstream: str | None,
    only: str | None,
    force: bool,
    rehash: bool,
    display_mode: RunDisplayMode,
    console: Console | None = None,
) -> int:
    context = resolve_entry_context(entry, pipeline, pipeline_args)
    configure_cli_logging()
    try:
        outcomes = run(
            context.pipeline,
            context.config,
            args=context.args,
            upto=upto,
            downstream=downstream,
            only=only,
            force=force,
            cli_out=context.output_base,
            branch=context.branch,
            is_temporary=context.is_temporary,
            temporary_config=context.resolved.temporary_config,
            axes=context.axes,
            temporary_axes=context.resolved.temporary_axes,
            graph=context.graph,
            display_mode=display_mode,
            rehash=rehash,
        )
    except ReviewRequiredError as error:
        print(str(error), file=sys.stderr)
        return 2
    render_run_outcomes(console or make_console(), outcomes, elapsed=True)
    return 0


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
    filtered = states
    if statuses:
        wanted = set(statuses)
        filtered = [state for state in states if state.status in wanted]
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
            from varve.dashboard.state import import_entry_pipeline

            pipeline = import_entry_pipeline(entry)
            context = resolve_entry_context(entry, pipeline, pipeline.Args())
            result = record_source_review(
                pipeline,
                context.config,
                decision=decision,
                args=context.args,
                cli_out=context.output_base,
                branch=context.branch,
                is_temporary=context.is_temporary,
                axes=context.axes,
                graph=context.graph,
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
            from varve.dashboard.state import import_entry_pipeline

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


def _run_context(context: ResolvedCommandContext, *, rehash: bool) -> None:
    run(
        context.pipeline,
        context.config,
        args=context.args,
        cli_out=context.output_base,
        branch=context.branch,
        is_temporary=context.is_temporary,
        temporary_config=context.resolved.temporary_config,
        axes=context.axes,
        temporary_axes=context.resolved.temporary_axes,
        graph=context.graph,
        rehash=rehash,
    )


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
    if not console.is_terminal:
        return nullcontext(None)
    return console.status(message, spinner="dots")
