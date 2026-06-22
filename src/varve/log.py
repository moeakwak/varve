"""Logging helpers for varve."""

from __future__ import annotations

import logging
import sys


def get_logger() -> logging.Logger:
    return logging.getLogger("varve")


def configure_cli_logging(verbose: bool = False) -> None:
    logger = get_logger()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("varve  %(message)s"))
        logger.addHandler(handler)
