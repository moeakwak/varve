"""The per-unit runtime context injected as ``ctx``."""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any, Generic, Protocol

from typing_extensions import TypeVar

from varve.axis import Cell

C = TypeVar("C", default=Any)
A = TypeVar("A", default=Any)
T = TypeVar("T")


class CtxSink(Protocol):
    """Receives live unit events; the engine installs one per unit."""

    def metric(self, ctx: Ctx[Any, Any], values: Mapping[str, Any]) -> None: ...

    def progress(
        self, ctx: Ctx[Any, Any], iterable: Iterable[T], total: int | None, desc: str | None
    ) -> Iterator[T]: ...


class NullSink:
    """Default sink: metrics are only kept on the context, progress passes through."""

    def metric(self, ctx: Ctx[Any, Any], values: Mapping[str, Any]) -> None:
        return None

    def progress(
        self, ctx: Ctx[Any, Any], iterable: Iterable[T], total: int | None, desc: str | None
    ) -> Iterator[T]:
        return iter(iterable)


class Ctx(Generic[C, A]):
    """Runtime context of one unit.

    ``out`` is the unit's (empty, private) output directory; ``coords`` maps
    every axis name to the unit's canonical id; ``cell`` holds static axis
    values. ``upstream(name)`` returns what an upstream stage or Ref would
    inject (a ``Path`` or a fan-in ``dict``), including ``needs=``-only ones.
    """

    def __init__(
        self,
        *,
        out: Path,
        config: C,
        args: A,
        branch: str,
        stage: str,
        unit_key: str = "",
        coords: Mapping[str, str] | None = None,
        cell: Cell | None = None,
        upstream: Mapping[str, Any] | None = None,
        sink: CtxSink | None = None,
    ) -> None:
        self.out = out
        self.config: C = config
        self.args: A = args
        self.branch = branch
        self.stage = stage
        self.unit_key = unit_key
        self.coords: Mapping[str, str] = dict(coords or {})
        self.cell = cell if cell is not None else Cell()
        self.metrics: dict[str, Any] = {}
        self._upstream = dict(upstream or {})
        self._sink: CtxSink = sink or NullSink()

    def upstream(self, name: str) -> Any:
        try:
            return self._upstream[name]
        except KeyError:
            known = ", ".join(sorted(self._upstream)) or "none"
            raise KeyError(
                f"Stage {self.stage!r} has no upstream {name!r} (declared: {known}); "
                "inject it as a parameter or list it in needs="
            ) from None

    def metric(self, **values: Any) -> None:
        """Record unit metrics (merged); ``items=N`` drives throughput views."""

        self.metrics.update(values)
        self._sink.metric(self, values)

    def progress(
        self, iterable: Iterable[T], total: int | None = None, desc: str | None = None
    ) -> Iterator[T]:
        """Iterate ``iterable`` while reporting progress to the live display."""

        if total is None:
            try:
                total = len(iterable)  # type: ignore[arg-type]
            except TypeError:
                total = None
        return self._sink.progress(self, iterable, total, desc)

    def __repr__(self) -> str:
        unit = f"@{self.unit_key}" if self.unit_key else ""
        return f"Ctx({self.stage}{unit}, branch={self.branch!r})"
