"""Stage decorators and their captured metadata."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from varve.keyspec import KeySpec

StageKind = Literal["single", "batch"]
ProducesItem = str | Path
ProducesSpec = (
    ProducesItem | list[ProducesItem] | Callable[[Any], ProducesItem | list[ProducesItem]] | None
)
NeedItem = str | Callable[..., Any]


@dataclass(frozen=True)
class StageSpec:
    name: str
    kind: StageKind
    func: Callable[..., Any]
    needs: tuple[str, ...]
    produces: ProducesSpec
    keyspec: KeySpec
    auto_uses: bool = True
    uses: tuple[Callable[..., Any], ...] = ()
    matrix: tuple[Any, ...] = ()
    logical_needs: tuple[str, ...] = ()
    cell: tuple[tuple[Any, Any], ...] = ()
    base_name: str | None = None
    need_cells: dict[str, tuple[str, ...]] | None = None

    def expanded(self, *, name: str, cell: tuple[tuple[Any, Any], ...]) -> StageSpec:
        return replace(
            self,
            name=name,
            logical_needs=self.needs,
            cell=cell,
            base_name=self.name if cell else None,
        )

    def with_wiring(
        self, needs: tuple[str, ...], need_cells: dict[str, tuple[str, ...]]
    ) -> StageSpec:
        return replace(self, needs=needs, need_cells=need_cells)


def _need_name(need: NeedItem) -> str:
    if isinstance(need, str):
        return need
    return need.__name__


def _normalize_needs(
    needs: NeedItem | list[NeedItem] | tuple[NeedItem, ...] | None,
) -> tuple[str, ...]:
    if needs is None:
        return ()
    if isinstance(needs, str) or callable(needs):
        return (_need_name(needs),)
    return tuple(_need_name(need) for need in needs)


def _attach_stage_spec(func: Callable[..., Any], spec: StageSpec) -> Callable[..., Any]:
    setattr(func, "__varve_stage__", spec)
    return func


def stage(
    *,
    needs: NeedItem | list[NeedItem] | tuple[NeedItem, ...] | None = None,
    produces: ProducesSpec = None,
    key: KeySpec | None = None,
    auto_uses: bool = True,
    uses: list[Callable[..., Any]] | tuple[Callable[..., Any], ...] = (),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a single-run stage.

    `needs` accepts stage names or method references defined earlier in the
    class body.
    """

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        spec = StageSpec(
            name=func.__name__,
            kind="single",
            func=func,
            needs=_normalize_needs(needs),
            produces=produces,
            keyspec=key if key is not None else KeySpec(),
            auto_uses=auto_uses,
            uses=tuple(uses),
        )
        return _attach_stage_spec(func, spec)

    return decorate


def batch_stage(
    *,
    needs: NeedItem | list[NeedItem] | tuple[NeedItem, ...] | None = None,
    produces: ProducesSpec = None,
    key: KeySpec | None = None,
    auto_uses: bool = True,
    uses: list[Callable[..., Any]] | tuple[Callable[..., Any], ...] = (),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare an async-generator batch stage.

    `needs` accepts stage names or method references defined earlier in the
    class body.
    """

    if produces is not None:
        raise ValueError(
            "batch_stage does not accept produces: batch outputs are recorded from the "
            "paths each batch yields, not from a static produces declaration."
        )

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        spec = StageSpec(
            name=func.__name__,
            kind="batch",
            func=func,
            needs=_normalize_needs(needs),
            produces=produces,
            keyspec=key if key is not None else KeySpec(),
            auto_uses=auto_uses,
            uses=tuple(uses),
        )
        return _attach_stage_spec(func, spec)

    return decorate


def matrix(*axes: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Expand a stage over the Cartesian product of the supplied axes."""
    if not axes:
        raise ValueError("matrix requires at least one Axis")
    if len({id(axis) for axis in axes}) != len(axes):
        raise ValueError("matrix axes must not repeat")

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        spec = getattr(func, "__varve_stage__", None)
        if spec is None:
            raise TypeError("@matrix must be stacked above @stage or @batch_stage")
        return _attach_stage_spec(func, replace(spec, matrix=tuple(axes)))

    return decorate
