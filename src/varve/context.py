"""Runtime context passed to stage methods."""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Any

from varve.store.store import Store


class Ctx:
    def __init__(
        self,
        *,
        config: Any,
        out: Path,
        store: Store | None = None,
        ledger: Store | None = None,
        resume_skip: frozenset[int] | None = None,
    ) -> None:
        self.config = config
        self.out = out
        if store is None:
            store = ledger  # Legacy keyword compatibility.
        if store is None:
            raise ValueError("Ctx requires a varve store")
        self._store = store
        self._resume_skip = resume_skip or frozenset()
        self._current_batch_index: int | None = None

    def input(self, stage: str) -> Path | list[Path]:
        record = self._store.read_success(stage)
        if record is None:
            raise ValueError(f"Upstream stage has no success record: {stage}")
        if record.kind == "single":
            assert record.produces is not None
            paths = [self.out / item.path for item in record.produces]
            return paths[0] if len(paths) == 1 else paths
        assert record.outputs is not None
        return [self.out / item.path for item in sorted(record.outputs, key=lambda item: item.index)]

    async def resume(self, iterable: Iterable[Any]) -> AsyncIterator[tuple[int, Any]]:
        for index, item in enumerate(iterable):
            if index in self._resume_skip:
                continue
            self._current_batch_index = index
            yield index, item
