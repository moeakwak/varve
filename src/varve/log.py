"""Logging helpers for varve."""

from __future__ import annotations

import logging
import sys
import time


def get_logger() -> logging.Logger:
    return logging.getLogger("varve")


def configure_cli_logging(verbose: bool = False) -> None:
    logger = get_logger()
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("varve  %(message)s"))
        logger.addHandler(handler)


class BatchProgress:
    def __init__(self, total: int, *, every: int = 10, min_interval: float = 1.0) -> None:
        self.total = total
        self.every = every
        self.min_interval = min_interval
        self._last = 0.0

    def tick(self, index: int) -> str | None:
        now = time.monotonic()
        is_last = index + 1 >= self.total
        should_emit = (index + 1) % self.every == 0 or is_last
        if should_emit and (now - self._last >= self.min_interval or is_last):
            self._last = now
            return f"batch {index + 1}/{self.total} done"
        return None

