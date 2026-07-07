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
    "artifact-missing": "yellow",
    "resume": "yellow",
    "no-cache": "yellow",
    "stale": "yellow",
    "run": "cyan",
    "dirty": "red",
    "error": "red",
}


# Style for the bracketed stage name in the live run log, e.g. "[render_ablation]".
STAGE_STYLE = "bold"

# Leading glyph and styling for a `refresh <pipeline> --branch <branch>` header.
# The header groups the stage lines that follow it, so it gets its own accent.
REFRESH_MARKER = "▸"
REFRESH_STYLE = "bold cyan"


def _theme_key(status: str) -> str:
    # Theme style names and regex group names must be valid identifiers.
    return status.replace("-", "_")


_THEME = Theme(
    {f"varve.{_theme_key(status)}": style for status, style in STATUS_STYLES.items()}
    | {"varve.stage": STAGE_STYLE, "varve.refresh": REFRESH_STYLE}
)


class VarveStatusHighlighter(RegexHighlighter):
    """Color the stage name and status tokens in the live run log."""

    base_style = "varve."
    highlights = [
        # Accent the whole `▸ refresh <pipeline> --branch <branch>` header.
        rf"(?P<refresh>{re.escape(REFRESH_MARKER)} refresh .+)",
        r"(?P<stage>\[[^\]]+\])",
        *(rf"(?P<{_theme_key(status)}>\b{re.escape(status)}\b)" for status in STATUS_STYLES),
    ]


def make_console(*, stderr: bool = False) -> Console:
    """Build a Console sharing the varve theme with auto-highlight disabled."""

    return Console(stderr=stderr, theme=_THEME, highlight=False)


def status_text(status: str) -> Text:
    """Return the status token styled for its semantic color."""

    return Text(status, style=STATUS_STYLES.get(status, ""))
