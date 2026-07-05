"""Runtime context passed to stage methods."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Iterable
from pathlib import Path
from typing import Any, Generic, TypeVar, cast

from varve.store.store import Store

ConfigT = TypeVar("ConfigT")
ArgsT = TypeVar("ArgsT")


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


class Ctx(Generic[ConfigT, ArgsT]):
    """Runtime context passed to stage methods.

    `config`, `args`, and `out` expose the pipeline inputs and output root. Use
    `input()` when an upstream stage must have exactly one output, `inputs()` when
    it may have many, and `resume()` to iterate batch work with automatic resume.
    """

    def __init__(
        self,
        *,
        config: ConfigT,
        args: ArgsT | None = None,
        out: Path,
        store: Store,
        resume_skip: frozenset[int] | None = None,
        stage_name: str | None = None,
        declared_needs: frozenset[str] | None = None,
    ) -> None:
        self.config: ConfigT = config
        self.args: ArgsT = cast(ArgsT, args)
        self.out = out
        self._store = store
        self._resume_skip = resume_skip or frozenset()
        self._stage_name = stage_name
        self._declared_needs = declared_needs
        self._current_batch_index: int | None = None
        self._used_resume = False

    def _check_declared_need(self, stage: str) -> None:
        if self._declared_needs is None or stage in self._declared_needs:
            return
        current = f" for stage {self._stage_name!r}" if self._stage_name else ""
        raise ValueError(
            f"Cannot read upstream stage {stage!r}{current}: declare it in needs= so "
            "the upstream content key is part of this stage's key."
        )

    def _input_paths(self, stage: str) -> list[Path]:
        self._check_declared_need(stage)
        record = self._store.read_success(stage)
        if record is None:
            raise ValueError(f"Upstream stage has no success record: {stage}")
        if record.kind == "single":
            assert record.produces is not None
            return [self.out / item.path for item in record.produces]
        assert record.outputs is not None
        return [
            self.out / item.path for item in sorted(record.outputs, key=lambda item: item.index)
        ]

    def input(self, stage: str) -> Path:
        """Return the single output path produced by an upstream stage.

        Raises when the upstream stage produced zero or multiple paths. Use
        `inputs(stage)` for upstream stages that can produce more than one path.
        """

        paths = self._input_paths(stage)
        if len(paths) != 1:
            raise ValueError(
                f"Expected exactly one output from upstream stage {stage!r}, "
                f"found {len(paths)}. Use ctx.inputs({stage!r}) for multiple outputs."
            )
        return paths[0]

    def inputs(self, stage: str) -> list[Path]:
        """Return all output paths produced by an upstream stage."""

        return self._input_paths(stage)

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
        """Iterate batch items, skipping items already recorded in a resumable run.

        Resume is positional: `iterable` must have deterministic order for the
        same keyed inputs. Sort unstable sources before passing them here. Batch
        stages do not validate per-item output shape; validate that in a
        downstream stage when shape matters.
        """

        self._used_resume = True
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
