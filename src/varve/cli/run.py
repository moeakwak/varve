"""Shared Rich rendering for single-pipeline run outcomes."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table
from rich.text import Text

from varve.engine.run_display import StageOutcome, status_counts
from varve.style import format_elapsed, status_text


def render_run_outcomes(
    console: Console,
    outcomes: list[StageOutcome],
) -> None:
    """Render the same outcome table for generated and top-level runs."""

    compact: dict[str, list[StageOutcome]] = {}
    for outcome in outcomes:
        if outcome.display_compact and outcome.display_base is not None:
            compact.setdefault(outcome.display_base, []).append(outcome)
    has_groups = bool(compact)
    table = Table(box=None)
    table.add_column("STAGE")
    table.add_column("STATUS")
    table.add_column("REASON")
    if has_groups:
        table.add_column("CELLS", justify="right")
        table.add_column("RAN", justify="right")
    table.add_column("ELAPSED", justify="right")
    for outcome in outcomes:
        grouped = outcome.display_compact
        base = outcome.display_base
        group = compact.pop(base, None) if grouped and base is not None else None
        if grouped and group is None:
            continue
        if group is not None:
            counts = status_counts(group)
            status = Text()
            for index, (status_name, count) in enumerate(counts):
                if index:
                    status.append(", ")
                status.append(f"{count} ")
                status.append_text(status_text(status_name))
            ran = sum(item.elapsed is not None for item in group)
            elapsed = sum(item.elapsed or 0.0 for item in group) if ran else None
        else:
            status = status_text(outcome.status)
            counts = ((outcome.status, 1),)
            ran = int(outcome.elapsed is not None)
            elapsed = outcome.elapsed
        row = [base or outcome.stage, status, "-" if grouped else outcome.reason]
        if has_groups:
            row.extend([str(outcome.display_cells), str(ran)] if grouped else ["-", str(ran)])
        row.append(format_elapsed(elapsed, missing="-"))
        table.add_row(*row)
    console.print(table)
