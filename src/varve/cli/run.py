"""Shared Rich rendering for single-pipeline run outcomes."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.text import Text

from varve.engine.run_display import StageOutcome, outcome_rows
from varve.style import format_elapsed, status_text


def render_run_outcomes(
    console: Console,
    outcomes: list[StageOutcome],
) -> None:
    """Render the same outcome table for generated and top-level runs."""

    rows = outcome_rows(outcomes)
    has_groups = any(row.grouped for row in rows)
    table = Table(box=None)
    table.add_column("STAGE")
    table.add_column("STATUS")
    table.add_column("REASON")
    if has_groups:
        table.add_column("CELLS", justify="right")
        table.add_column("RAN", justify="right")
    table.add_column("ELAPSED", justify="right")
    for outcome in rows:
        if outcome.grouped:
            status = Text()
            for index, (status_name, count) in enumerate(outcome.status_counts):
                if index:
                    status.append(", ")
                status.append(f"{count} ")
                status.append_text(status_text(status_name))
        else:
            status = status_text(outcome.status)
        row = [outcome.stage, status, outcome.reason]
        if has_groups:
            row.extend(
                [str(outcome.cells), str(outcome.ran)]
                if outcome.grouped
                else ["-", str(outcome.ran)]
            )
        row.append(format_elapsed(outcome.elapsed, missing="-"))
        table.add_row(*row)
    console.print(table)
