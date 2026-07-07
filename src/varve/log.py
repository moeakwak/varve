"""Logging helpers for varve."""

from __future__ import annotations

import logging

from rich.logging import RichHandler

from varve.style import VarveStatusHighlighter, make_console


def configure_cli_logging(verbose: bool = False, *, quiet: bool = False) -> None:
    logger = logging.getLogger("varve")
    # The live per-stage progress log is a `run` concern. Read-only commands like
    # `status` already print a table, so they stay quiet unless `-v` is given.
    if verbose:
        level = logging.DEBUG
    elif quiet:
        level = logging.WARNING
    else:
        level = logging.INFO
    logger.setLevel(level)
    if not logger.handlers:
        # RichHandler adds a colored timestamp column and lets the highlighter
        # tint status tokens (hit/run/done/...) by their semantic color, while
        # keeping the stream on stderr so verbosity and piping stay unchanged.
        handler = RichHandler(
            console=make_console(stderr=True),
            highlighter=VarveStatusHighlighter(),
            show_time=True,
            show_level=False,
            show_path=False,
            markup=False,
            log_time_format="%H:%M:%S",
            # Stamp every line, including consecutive stage/refresh lines that
            # land in the same second. Rich collapses repeated timestamps by
            # default, which hides when each stage actually ran.
            omit_repeated_times=False,
        )
        # No text prefix: the dim timestamp column already marks varve's lines.
        logger.addHandler(handler)
