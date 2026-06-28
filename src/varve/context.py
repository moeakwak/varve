"""Runtime context passed to stage methods."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path
from typing import Any

from varve.store.store import Store


def _len_or_none(iterable: Iterable[Any]) -> int | None:
    try:
        return len(iterable)  # type: ignore[arg-type]
    except TypeError:
        return None


def _make_tqdm_progress(
    *,
    desc: str,
    total: int | None,
    initial: int,
    unit: str,
):
    from tqdm.auto import tqdm

    return tqdm(total=total, initial=initial, desc=desc, unit=unit)


class _ResumeProgress:
    def __init__(
        self,
        *,
        desc: str,
        total: int | None,
        initial: int,
        unit: str,
    ) -> None:
        self._bar = _make_tqdm_progress(desc=desc, total=total, initial=initial, unit=unit)

    def update(self) -> None:
        self._bar.update(1)

    def set_postfix(self, text: str) -> None:
        self._bar.set_postfix_str(text)

    def close(self) -> None:
        self._bar.close()


class Ctx:
    def __init__(
        self,
        *,
        config: Any,
        args: Any = None,
        out: Path,
        store: Store | None = None,
        ledger: Store | None = None,
        resume_skip: frozenset[int] | None = None,
        stage_name: str | None = None,
    ) -> None:
        self.config = config
        self.args = args
        self.out = out
        if store is None:
            store = ledger  # Legacy keyword compatibility.
        if store is None:
            raise ValueError("Ctx requires a varve store")
        self._store = store
        self._resume_skip = resume_skip or frozenset()
        self._stage_name = stage_name
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
        return [
            self.out / item.path for item in sorted(record.outputs, key=lambda item: item.index)
        ]

    async def resume(
        self,
        iterable: Iterable[Any],
        *,
        progress: bool = True,
        desc: str | None = None,
        total: int | None = None,
        unit: str = "batch",
        postfix: Callable[[Any], str] | None = None,
    ) -> AsyncIterator[tuple[int, Any]]:
        progress_handle: _ResumeProgress | None = None
        if progress:
            label = desc or self._stage_name or "batch"
            inferred_total = total if total is not None else _len_or_none(iterable)
            initial = (
                sum(1 for index in self._resume_skip if index < inferred_total)
                if inferred_total is not None
                else 0
            )
            progress_handle = _ResumeProgress(
                desc=label,
                total=inferred_total,
                initial=initial,
                unit=unit,
            )
        try:
            for index, item in enumerate(iterable):
                if index in self._resume_skip:
                    continue
                self._current_batch_index = index
                yield index, item
                if progress_handle is not None:
                    if postfix is not None:
                        progress_handle.set_postfix(postfix(item))
                    progress_handle.update()
        finally:
            if progress_handle is not None:
                progress_handle.close()
