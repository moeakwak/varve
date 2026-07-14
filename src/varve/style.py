"""Shared Rich styling for varve CLI and dashboard output.

This module is the single source of truth for status colors so the generated
``Pipeline.cli()`` commands and the top-level ``varve`` dashboard render the
same way. It depends only on ``rich`` and deliberately avoids engine or
dashboard imports so it can sit at the leaf of the import graph.
"""

from __future__ import annotations

import re

from rich.console import Console
from rich.highlighter import RegexHighlighter
from rich.text import Text
from rich.theme import Theme

# Status token -> Rich style. Covers persisted stage/pipeline statuses plus the
# transient lifecycle words ("run", "done") emitted on the live run log.
STATUS_STYLES: dict[str, str] = {
    "hit": "green",
    "done": "green",
    "needs-review": "bold yellow",
    "needs-run": "yellow",
    "resume": "yellow",
    "run": "cyan",
    "failed": "red",
    "error": "red",
}

REVIEW_STYLES: dict[str, str] = {
    "accept_heading": "bold green",
    "accept_action": "green",
    "accept_already": "dim green",
    "reject_heading": "bold yellow",
    "reject_action": "yellow",
    "reject_already": "dim yellow",
    "stage": "cyan",
    "module": "blue",
    "branch": "dim",
    "noop": "dim",
    "error": "red",
    "total": "bold",
}


# Leading glyph and styling for a bulk `run <module> --branch <branch>` header.
# The header groups the stage lines that follow it, so it gets its own accent.
BULK_RUN_MARKER = "▸"

DEPENDENCY_STYLES = {
    "stage": "bold cyan",
    "function": "cyan",
    "class": "magenta",
    "module": "blue",
    "value": "green",
    "broad": "yellow",
    "metadata": "dim",
    "changed": "yellow",
    "added": "green",
    "removed": "red",
}


def _theme_key(status: str) -> str:
    # Theme style names and regex group names must be valid identifiers.
    return status.replace("-", "_")


_THEME = Theme(
    {f"varve.{_theme_key(status)}": style for status, style in STATUS_STYLES.items()}
    | {f"varve.dependency.{kind}": style for kind, style in DEPENDENCY_STYLES.items()}
    | {f"varve.review.{name}": style for name, style in REVIEW_STYLES.items()}
    | {"varve.stage": "bold", "varve.bulk_run": "bold cyan"}
)


class VarveStatusHighlighter(RegexHighlighter):
    """Color the stage name and status tokens in the live run log."""

    base_style = "varve."
    highlights = [
        # Accent the whole `▸ run <module> --branch <branch>` header.
        rf"(?P<bulk_run>{re.escape(BULK_RUN_MARKER)} run .+)",
        r"(?P<stage>\[[^\]]+\])",
        *(rf"(?P<{_theme_key(status)}>\b{re.escape(status)}\b)" for status in STATUS_STYLES),
    ]


def format_elapsed(value: float | None, *, missing: str = "") -> str:
    """Format persisted or live stage duration for CLI tables."""

    return f"{value:.2f}s" if value is not None else missing


def make_console(*, stderr: bool = False) -> Console:
    """Build a Console sharing the varve theme with auto-highlight disabled."""

    return Console(stderr=stderr, theme=_THEME, highlight=False)


def status_text(status: str) -> Text:
    """Return the status token styled for its semantic color."""

    return Text(status, style=STATUS_STYLES.get(status, ""))
