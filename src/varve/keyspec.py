"""Key declarations used to build content-addressed stage keys."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

type JSON = dict[str, JSON] | list[JSON] | str | int | float | bool | None


@dataclass(frozen=True)
class KeySpec:
    """Declarative inputs that affect a stage's durable outputs.

    The object deliberately stores callables and is therefore a dataclass rather
    than a pydantic model. The store persists the evaluated values, not this
    declaration object.
    """

    config: tuple[str, ...] = ()
    files: Mapping[str, Callable[[Any], Path | list[Path]]] = field(default_factory=dict)
    values: Mapping[str, Callable[[Any], JSON]] = field(default_factory=dict)

    @classmethod
    def coerce(cls, key: list[str] | tuple[str, ...] | KeySpec | None) -> KeySpec:
        if key is None:
            return cls()
        if isinstance(key, cls):
            return key
        if isinstance(key, list | tuple):
            return cls(config=tuple(key))
        raise TypeError(f"Unsupported key spec: {type(key).__name__}")
