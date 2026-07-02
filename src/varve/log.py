"""Logging helpers for varve."""

from __future__ import annotations

import logging
import sys


def configure_cli_logging(verbose: bool = False) -> None:
    logger = logging.getLogger("varve")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("varve  %(message)s"))
        logger.addHandler(handler)
