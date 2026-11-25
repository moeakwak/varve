"""Resources: shared sessions with dependency fingerprints and preflight checks."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generic, Literal, TypeVar

from varve.axis import Cell, check_identifier
from varve.dependencies import DepCtx, Dependencies

T = TypeVar("T")
ResourceScope = Literal["stage", "worker", "unit"]


@dataclass(frozen=True)
class RunCtx:
    """Context for run-time callables: ``workers=``, ``Resource.preflight/open``.

    ``cell`` is empty for stage-level calls and holds the unit's static
    coordinates for ``scope="unit"`` resources.
    """

    config: Any
    args: Any
    branch: str
    stage: str
    root: Path
    cell: Cell = field(default_factory=Cell)


@dataclass(frozen=True)
class Resource(Generic[T]):
    """A named session injected into stages by parameter name.

    ``depends`` contributes inputs, values, and sources to every stage that
    uses the resource; ``preflight`` runs once per run before the first unit
    needing it; ``open`` returns a (sync or async) context manager yielding the
    session, opened once per ``scope``.
    """

    name: str
    depends: Dependencies | Callable[[DepCtx], Dependencies] = Dependencies()
    preflight: Callable[[RunCtx], None] | None = None
    open: Callable[[RunCtx], AbstractContextManager[T] | AbstractAsyncContextManager[T]] | None = (
        None
    )
    scope: ResourceScope = "stage"

    def __post_init__(self) -> None:
        check_identifier(self.name, "Resource")
        if self.scope not in ("stage", "worker", "unit"):
            raise ValueError(f"Resource {self.name!r}: scope must be stage, worker, or unit")
