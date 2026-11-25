"""Shared Rich styling for varve CLI, TUI, and plain run logs.

This module is the single source of truth for status colors so the generated
``Pipeline.cli()`` commands, the live TUI, and the top-level ``varve`` command
render the same way. It depends only on ``rich`` and sits at the leaf of the
import graph.
"""

from __future__ import annotations

import os
import re
import sys
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import IO, Any, cast

from rich.console import Console
from rich.highlighter import RegexHighlighter
from rich.text import Text
from rich.theme import Theme

# Status token -> Rich style. Covers unit decisions (hit/needs-run/failed),
# stage-level probe states (awaiting/blocked), stored unit statuses
# (running/ok/orphaned), display labels (linked/reused/code-modified), and the
# lifecycle words of the plain run log (run/done/skipped/interrupted).
STATUS_STYLES: dict[str, str] = {
    "hit": "green",
    "ok": "green",
    "done": "green",
    "linked": "green",
    "reused": "dim green",
    "code-modified": "dim yellow",
    "needs-run": "yellow",
    "awaiting": "magenta",
    "run": "cyan",
    "running": "cyan",
    "failed": "red",
    "error": "red",
    "blocked": "red",
    "interrupted": "yellow",
    "skipped": "dim",
    "orphaned": "dim",
    "uninitialized": "dim",
}

# Styles for segment / source diffs shown by `inspect`.
DIFF_STYLES = {
    "segment": "bold cyan",
    "symbol": "cyan",
    "metadata": "dim",
    "changed": "yellow",
    "added": "green",
    "removed": "red",
}

# Leading glyph of a bulk `run <module> --branch <branch>` header line.
BULK_RUN_MARKER = "▸"


def _theme_key(status: str) -> str:
    # Theme style names and regex group names must be valid identifiers.
    return status.replace("-", "_")


_THEME = Theme(
    {f"varve.{_theme_key(status)}": style for status, style in STATUS_STYLES.items()}
    | {f"varve.diff.{kind}": style for kind, style in DIFF_STYLES.items()}
    | {"varve.stage": "bold", "varve.bulk_run": "bold cyan"}
)


class VarveStatusHighlighter(RegexHighlighter):
    """Color the stage name and status tokens in the plain run log."""

    base_style = "varve."
    highlights = [
        rf"(?P<bulk_run>{re.escape(BULK_RUN_MARKER)} run .+)",
        r"(?P<stage>\[[^\]]+\])",
        r"(?P<hit>✓)",
        *(
            rf"(?P<{_theme_key(status)}>(?<![\w-]){re.escape(status)}(?![\w-]))"
            for status in STATUS_STYLES
        ),
    ]


def format_elapsed(value: float | None, *, missing: str = "") -> str:
    """Format a duration in seconds for CLI tables."""

    return f"{value:.2f}s" if value is not None else missing


def format_timestamp(value: str | None, *, missing: str = "-") -> str:
    """Render a stored UTC ISO timestamp in local time as ``YYYY-MM-DD HH:MM:SS``."""

    if not value:
        return missing
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value[:19]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def format_ms(value: int | float | None, *, missing: str = "—") -> str:
    """Format a duration in milliseconds compactly (``850ms``, ``12.3s``, ``4m05s``)."""

    if value is None:
        return missing
    if value < 1000:
        return f"{int(value)}ms"
    seconds = value / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


class PipeSafeStream:
    """The current ``sys.stdout``/``sys.stderr``, tolerating a closed pipe.

    After the first ``BrokenPipeError`` (``varve status | head``) output is
    dropped and the file descriptor is pointed at ``/dev/null``, so the command
    keeps going (a run keeps committing) and the interpreter's exit flush does
    not fail either.
    """

    def __init__(self, *, stderr: bool = False) -> None:
        self.stderr = stderr
        self.broken = False

    @property
    def target(self) -> Any:
        return sys.stderr if self.stderr else sys.stdout

    def _break(self) -> None:
        self.broken = True
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, self.target.fileno())
            finally:
                os.close(devnull)
        except (OSError, ValueError, AttributeError):
            pass

    def write(self, text: str) -> int:
        if not self.broken:
            try:
                return self.target.write(text)
            except BrokenPipeError:
                self._break()
        return len(text)

    def flush(self) -> None:
        if not self.broken:
            try:
                self.target.flush()
            except BrokenPipeError:
                self._break()

    def isatty(self) -> bool:
        return not self.broken and self.target.isatty()

    def fileno(self) -> int:
        return self.target.fileno()

    @property
    def encoding(self) -> str:
        return getattr(self.target, "encoding", None) or "utf-8"

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target, name)


def make_console(*, stderr: bool = False) -> Console:
    """Build a Console sharing the varve theme with auto-highlight disabled.

    It writes through :class:`PipeSafeStream`, so a closed stdout/stderr pipe
    silences the console instead of raising.
    """

    stream = cast(IO[str], PipeSafeStream(stderr=stderr))
    return Console(file=stream, theme=_THEME, highlight=False)


def loading(console: Console, message: str):
    return console.status(message, spinner="dots") if console.is_terminal else nullcontext(None)


def status_text(status: str) -> Text:
    """Return the status token styled for its semantic color."""

    return Text(status, style=STATUS_STYLES.get(status.split("@", 1)[0], ""))
