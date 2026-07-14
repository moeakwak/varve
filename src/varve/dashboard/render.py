"""Rich renderers for exact overview and bulk run states."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from rich.console import Console
from rich.table import Table
from rich.text import Text

from varve.dashboard.models import PipelineState
from varve.style import format_elapsed, make_console, status_text


def render_overview(
    states: Sequence[PipelineState],
    *,
    console: Console | None = None,
) -> None:
    """Render complete manifest modules in a wide table or narrow stacked rows."""

    console = console or make_console()
    modules = [_module(state) for state in states]
    wide = console.width >= max(100, max((len(module) for module in modules), default=0) + 48)
    if not wide:
        for index, state in enumerate(states):
            if index:
                console.print()
            console.print(_module(state), style="blue", soft_wrap=True)
            metadata = Text("  ")
            metadata.append(state.entry.branch, style="dim")
            metadata.append("  ")
            metadata.append_text(status_text(state.status))
            if state.duration is not None:
                metadata.append(f"  {format_elapsed(state.duration)}", style="dim")
            if state.last_run is not None:
                metadata.append(f"  {_format_datetime(state.last_run)}", style="dim")
            console.print(metadata)
        return

    table = Table(box=None)
    table.add_column("MODULE", no_wrap=True, overflow="ignore")
    table.add_column("BRANCH")
    table.add_column("STATUS")
    table.add_column("DURATION", justify="right")
    table.add_column("LAST RUN")
    for state in states:
        table.add_row(
            _module(state),
            state.entry.branch,
            status_text(state.status),
            format_elapsed(state.duration, missing="-"),
            _format_datetime(state.last_run),
        )
    console.print(table)


def render_no_status_matches(console: Console | None = None) -> None:
    (console or make_console()).print("No pipelines match the selected statuses.")


def render_bulk_run(
    states: Sequence[PipelineState],
    *,
    console: Console | None = None,
) -> None:
    """Render every final incomplete category from fresh exact states."""

    console = console or make_console()
    incomplete = [state for state in states if not state.complete]
    if not incomplete:
        console.print("All selected pipelines are complete.")
        return
    console.print("Run incomplete")

    stage_states = [(state, stage) for state in incomplete for stage in state.stages]
    reviews = [item for item in stage_states if item[1].status == "needs-review"]
    failures = [item for item in stage_states if item[1].status == "failed"]
    pipeline_errors = [state for state in incomplete if state.error is not None]
    stage_errors = [item for item in stage_states if item[1].status == "error"]
    to_run = [item for item in stage_states if item[1].status in {"needs-run", "resume"}]

    if reviews:
        console.print("\nTO REVIEW", style="bold yellow")
        seen: set[tuple[str, str, str]] = set()
        for state, stage in reviews:
            key = (_module(state), state.entry.branch, stage.base_name)
            if key in seen:
                continue
            seen.add(key)
            console.print(f"{key[0]}  {key[1]}  {key[2]}")
    if failures:
        console.print("\nFAILED", style="bold red")
        for state, stage in failures:
            console.print(
                f"{_module(state)}  {state.entry.branch}  {stage.name}  "
                f"{stage.failure or stage.reason}"
            )
    if pipeline_errors or stage_errors:
        console.print("\nERROR", style="bold red")
        for state in pipeline_errors:
            assert state.error is not None
            console.print(
                f"{_module(state)}  {state.entry.branch}  "
                f"{state.error.phase}  {state.error.message}"
            )
        for state, stage in stage_errors:
            console.print(
                f"{_module(state)}  {state.entry.branch}  evaluate  {stage.name}: {stage.reason}"
            )
    if to_run:
        console.print("\nTO RUN", style="bold yellow")
        for state, stage in to_run:
            console.print(
                f"{_module(state)}  {state.entry.branch}  {stage.name}  "
                f"{stage.status}  {stage.reason}"
            )


def _module(state: PipelineState) -> str:
    return state.entry.module or f"<manifest error: {state.entry.pipeline_id}>"


def _format_datetime(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value is not None else "-"
