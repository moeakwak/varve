"""Batched store writes: unit rows, file sets and stat-cache rows per time window."""

from __future__ import annotations

import time
from collections.abc import Iterable

from varve.keying.fingerprint import StatCache
from varve.keying.keys import FileSetRow
from varve.store.repo import Repo, UnitRow

FLUSH_SECONDS = 0.075


class Writer:
    """Collects writes and commits them in one transaction every ``FLUSH_SECONDS``.

    ``running`` marks must reach the store before their unit replaces an
    existing unit dir (startup recovery finds interrupted promotions through
    them), so :meth:`before_promote` flushes when the unit's mark is still
    pending. Marks put with ``sync=True`` (units that already have a row) are
    also flushed by :meth:`flush_running` before their unit bodies start, so a
    hard kill leaves those rows ``running`` rather than their previous state.
    """

    def __init__(self, repo: Repo, cache: StatCache) -> None:
        self.repo = repo
        self.cache = cache
        self.interval = FLUSH_SECONDS
        self._units: dict[tuple[str, str], UnitRow] = {}
        self._file_sets: dict[str, FileSetRow] = {}
        self._pending_running: set[tuple[str, str]] = set()
        self._sync_running = False
        self._last = time.monotonic()

    def put(
        self, row: UnitRow, file_sets: Iterable[FileSetRow] = (), *, sync: bool = False
    ) -> None:
        key = (row.stage_name, row.unit_key)
        self._units[key] = row
        if row.status == "running":
            self._pending_running.add(key)
            self._sync_running = self._sync_running or sync
        else:  # the pending row is no longer a running mark
            self._pending_running.discard(key)
        for item in file_sets:
            self._file_sets[item.fileset_hash] = item

    def before_promote(self, stage: str, unit_key: str) -> None:
        if (stage, unit_key) in self._pending_running:
            self.flush()

    def flush_running(self) -> None:
        """Flush pending ``sync`` running marks (called before unit bodies start)."""

        if self._sync_running:
            self.flush()

    def maybe_flush(self) -> None:
        if time.monotonic() - self._last >= self.interval:
            self.flush()

    def flush(self) -> None:
        self._last = time.monotonic()
        stats = self.cache.drain()
        if not (self._units or self._file_sets or stats):
            return
        with self.repo.transaction():
            if self._file_sets:
                self.repo.put_file_sets(self._file_sets.values())
            if self._units:
                self.repo.upsert_units(self._units.values())
            if stats:
                self.repo.put_stat_cache(stats)
        self._units.clear()
        self._file_sets.clear()
        self._pending_running.clear()
        self._sync_running = False
