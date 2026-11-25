"""Axes: static ordered domains and dynamic resolver-driven shards."""

from __future__ import annotations

import keyword
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

AxisKey = str | int | Enum
InputFingerprint = Literal["content", "stat"]

_RESERVED_NAMES = frozenset({"self", "ctx"})
_FORBIDDEN_ID_CHARS = frozenset("/,=\0")


def check_identifier(name: str, what: str) -> str:
    """Validate a name that is also used as a stage parameter name."""

    if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
        raise ValueError(f"{what} name must be a Python identifier: {name!r}")
    if name in _RESERVED_NAMES:
        raise ValueError(f"{what} name {name!r} is reserved")
    return name


def canonical_id(value: object) -> str:
    """Return the canonical string id of an axis key.

    Enum members use their value (str, or int rendered as str) and fall back to
    the member name; ints render as decimal; strings are used as-is. Ids must be
    non-empty, must not be ``.`` or ``..``, and must not contain ``/``, ``,``,
    ``=`` or NUL, so that ``unit_key`` strings and selectors stay unambiguous.
    """

    if isinstance(value, Enum):
        raw = value.value
        result = (
            raw
            if isinstance(raw, str)
            else str(raw)
            if isinstance(raw, int) and not isinstance(raw, bool)
            else value.name
        )
    elif isinstance(value, bool):
        raise TypeError("Axis keys must be str, int, or Enum members, not bool")
    elif isinstance(value, str):
        result = value
    elif isinstance(value, int):
        result = str(value)
    else:
        raise TypeError(f"Axis keys must be str, int, or Enum members, got {type(value).__name__}")
    if not result or result in (".", "..") or _FORBIDDEN_ID_CHARS.intersection(result):
        raise ValueError(
            f"Invalid axis id {result!r}: ids must be non-empty, not '.'/'..', "
            "and must not contain '/', ',', '=' or NUL"
        )
    return result


class Axis:
    """One named dimension of a stage's ``foreach``.

    A static axis declares an ordered tuple of ``values`` (str, int, or Enum
    members). A dynamic axis declares a ``resolver`` whose parameters are bound
    by name like a stage's (``ctx``, preceding static axes, upstream stages,
    Refs) and returns either ``Mapping[key, Path]`` (each unit receives the
    path, which enters its input key), ``Mapping[key, value]`` or
    ``Sequence[key]``. Axes are matched across stages by object identity.
    """

    __slots__ = ("name", "values", "resolver", "input_fingerprint", "_static")

    def __init__(
        self,
        name: str,
        values: Sequence[AxisKey] | None = None,
        *,
        resolver: Callable[..., Mapping[Any, Any] | Sequence[Any]] | None = None,
        input_fingerprint: InputFingerprint = "content",
    ) -> None:
        check_identifier(name, "Axis")
        if (values is None) == (resolver is None):
            raise ValueError(f"Axis {name!r} needs exactly one of values= or resolver=")
        if input_fingerprint not in ("content", "stat"):
            raise ValueError(f"Axis {name!r}: input_fingerprint must be 'content' or 'stat'")
        if values is not None and input_fingerprint != "content":
            raise ValueError(f"Axis {name!r}: input_fingerprint applies to dynamic axes only")
        self.name = name
        self.resolver = resolver
        self.input_fingerprint: InputFingerprint = input_fingerprint
        self.values: tuple[AxisKey, ...] | None = None
        self._static: AxisDomain | None = None
        if values is not None:
            if isinstance(values, str | bytes) or not isinstance(values, Sequence):
                raise TypeError(f"Axis {name!r}: values must be a sequence of keys")
            if not values:
                raise ValueError(f"Axis {name!r} must declare at least one value")
            self.values = tuple(values)
            self._static = AxisDomain.build(self, self.values, self.values)

    @property
    def is_static(self) -> bool:
        return self._static is not None

    @property
    def ids(self) -> tuple[str, ...]:
        """Canonical ids of a static axis in declaration order."""

        return self.static_domain().ids

    def static_domain(self) -> AxisDomain:
        if self._static is None:
            raise TypeError(f"Axis {self.name!r} is dynamic and has no static domain")
        return self._static

    def domain_from_resolved(self, resolved: object) -> AxisDomain:
        """Normalize a resolver result into an ordered domain."""

        if isinstance(resolved, Mapping):
            keys = tuple(resolved)
            values = tuple(resolved.values())
            paths = [isinstance(value, Path) for value in values]
            if any(paths) and not all(paths):
                raise TypeError(
                    f"Axis {self.name!r} resolver mixed Path and non-Path values; "
                    "return all paths or no paths"
                )
        elif isinstance(resolved, Sequence) and not isinstance(resolved, str | bytes):
            keys = tuple(resolved)
            values = keys
        else:
            raise TypeError(
                f"Axis {self.name!r} resolver must return a Mapping or a Sequence of keys, "
                f"got {type(resolved).__name__}"
            )
        for key in keys:
            if isinstance(key, Enum) or not isinstance(key, str | int):
                raise TypeError(f"Axis {self.name!r} resolver keys must be str or int, got {key!r}")
        return AxisDomain.build(self, keys, values)

    def __repr__(self) -> str:
        if self.values is not None:
            return f"Axis({self.name!r}, {list(self.values)!r})"
        return f"Axis({self.name!r}, resolver=...)"


@dataclass(frozen=True, eq=False)
class AxisDomain:
    """The ordered items of one axis for one command.

    ``keys`` are fan-in dict keys (declared static values or resolver keys),
    ``ids`` their canonical strings, and ``values`` what a stage parameter of
    the axis' name receives.
    """

    axis: Axis
    keys: tuple[Any, ...]
    ids: tuple[str, ...]
    values: tuple[Any, ...]
    _index: Mapping[str, int] = field(repr=False)

    @classmethod
    def build(cls, axis: Axis, keys: Sequence[Any], values: Sequence[Any]) -> AxisDomain:
        ids = tuple(canonical_id(key) for key in keys)
        index = {item: position for position, item in enumerate(ids)}
        if len(index) != len(ids):
            duplicate = next(item for item in ids if ids.count(item) > 1)
            raise ValueError(f"Axis {axis.name!r} has duplicate id {duplicate!r}")
        return cls(axis, tuple(keys), ids, tuple(values), MappingProxyType(index))

    def __len__(self) -> int:
        return len(self.ids)

    def position(self, item_id: str) -> int:
        try:
            return self._index[item_id]
        except KeyError:
            raise ValueError(
                f"Unknown id {item_id!r} for axis {self.axis.name!r}; "
                f"expected one of {list(self.ids)!r}"
            ) from None

    def restrict(self, ids: Sequence[str]) -> AxisDomain:
        """Keep only ``ids`` (validated), preserving domain order."""

        wanted = {self.position(item) for item in ids}
        keep = sorted(wanted)
        return AxisDomain.build(
            self.axis,
            [self.keys[i] for i in keep],
            [self.values[i] for i in keep],
        )

    @property
    def is_path(self) -> bool:
        return bool(self.values) and isinstance(self.values[0], Path)


class Cell(Mapping[str, Any]):
    """Read-only mapping of static axis names to values; also attribute access."""

    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, Any] | None = None) -> None:
        self._values = dict(values or {})

    def __getitem__(self, name: str) -> Any:
        return self._values[name]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getattr__(self, name: str) -> Any:
        if name == "_values":  # not yet set while unpickling
            raise AttributeError(name)
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(name) from None

    def __reduce__(self) -> tuple[Any, ...]:
        return (Cell, (self._values,))

    def __repr__(self) -> str:
        return f"Cell({self._values!r})"
