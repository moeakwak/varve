"""Stage decorators and their captured metadata."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Literal

from varve.dependencies import Dependencies

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
    depends: Dependencies
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


def _attach_stage_spec(func: Callable[..., Any], spec: StageSpec) -> Callable[..., Any]:
    setattr(func, "__varve_stage__", spec)
    return func


def _normalize_needs(
    needs: NeedItem | list[NeedItem] | tuple[NeedItem, ...] | None,
) -> tuple[str, ...]:
    if needs is None:
        return ()
    if isinstance(needs, str) or callable(needs):
        needs = (needs,)
    return tuple(need if isinstance(need, str) else need.__name__ for need in needs)


def _stage_decorator(
    kind: StageKind,
    needs: NeedItem | list[NeedItem] | tuple[NeedItem, ...] | None,
    produces: ProducesSpec,
    depends: Dependencies | None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        return _attach_stage_spec(
            func,
            StageSpec(
                name=func.__name__,
                kind=kind,
                func=func,
                needs=_normalize_needs(needs),
                produces=produces,
                depends=depends if depends is not None else Dependencies(),
            ),
        )

    return decorate


def stage(
    *,
    needs: NeedItem | list[NeedItem] | tuple[NeedItem, ...] | None = None,
    produces: ProducesSpec = None,
    depends: Dependencies | None = None,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a single-run stage.

    `needs` accepts stage names or method references defined earlier in the
    class body.
    """

    return _stage_decorator("single", needs, produces, depends)


def batch_stage(
    *,
    needs: NeedItem | list[NeedItem] | tuple[NeedItem, ...] | None = None,
    produces: ProducesSpec = None,
    depends: Dependencies | None = None,
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

    return _stage_decorator("batch", needs, None, depends)


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
