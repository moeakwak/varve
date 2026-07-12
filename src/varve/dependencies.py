"""Explicit non-stage dependency declarations."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TypeAlias

JSON: TypeAlias = Mapping[str, "JSON"] | list["JSON"] | str | int | float | bool | None


@dataclass(frozen=True)
class DependencyContext:
    """Stable context available while resolving durable dependencies.

    Runtime ``Args`` are deliberately absent: a value that selects durable
    input or output semantics belongs in ``Config`` or a static declaration.
    """

    config: Any
    out: Path
    cell: Any
    cell_out: Path

    @property
    def args(self) -> Any:
        raise TypeError(
            "Dependencies resolvers cannot read Args; move durable values and input "
            "locations to Config"
        )


@dataclass(frozen=True)
class Dependencies:
    """Files, values, and Python source paths that affect a stage."""

    inputs: Mapping[str, Callable[[DependencyContext], Path | Sequence[Path]]] = field(
        default_factory=dict
    )
    values: Mapping[str, Callable[[DependencyContext], JSON]] = field(default_factory=dict)
    sources: Sequence[Path] = field(default_factory=tuple)


def merge_dependencies(base: Dependencies, stage: Dependencies) -> Dependencies:
    """Merge pipeline and stage declarations, rejecting ambiguous names."""

    duplicate_inputs = set(base.inputs) & set(stage.inputs)
    duplicate_values = set(base.values) & set(stage.values)
    if duplicate_inputs or duplicate_values:
        details = []
        if duplicate_inputs:
            details.append(f"inputs {sorted(duplicate_inputs)!r}")
        if duplicate_values:
            details.append(f"values {sorted(duplicate_values)!r}")
        raise ValueError("Duplicate pipeline and stage dependencies: " + ", ".join(details))
    sources = tuple(dict.fromkeys((*base.sources, *stage.sources)))
    return Dependencies(
        inputs={**base.inputs, **stage.inputs},
        values={**base.values, **stage.values},
        sources=sources,
    )
