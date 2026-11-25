"""Explicit non-stage dependency declarations."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, TypeAlias

from varve.axis import Cell

JSON: TypeAlias = Mapping[str, "JSON"] | Sequence["JSON"] | str | int | float | bool | None
FingerprintMode = Literal["content", "stat"]


@dataclass(frozen=True)
class DepCtx:
    """Context for dependency, resource, and resolver callables.

    Only durable inputs are visible: ``config`` and the static ``cell`` of the
    unit being keyed. Runtime ``Args`` are deliberately absent.
    """

    config: Any
    cell: Cell = field(default_factory=Cell)


@dataclass(frozen=True)
class PathSet:
    """Explicit files or directories (directories are walked recursively)."""

    paths: tuple[Path, ...]
    mode: FingerprintMode = "content"


@dataclass(frozen=True)
class Glob:
    """Files under ``root`` matching ``include`` and not ``exclude`` (POSIX globs, ``**``)."""

    root: Path
    include: tuple[str, ...] = ("**",)
    exclude: tuple[str, ...] = ()
    allow_empty: bool = False
    mode: FingerprintMode = "content"


InputSpec: TypeAlias = Path | str | Sequence[Path | str] | PathSet | Glob


def _strs(value: str | Sequence[str]) -> tuple[str, ...]:
    return (value,) if isinstance(value, str) else tuple(value)


def as_input_set(spec: InputSpec) -> PathSet | Glob:
    """Normalize any accepted ``inputs`` value into a ``PathSet`` or ``Glob``."""

    if isinstance(spec, PathSet | Glob):
        return spec
    if isinstance(spec, str | Path):
        return PathSet((Path(spec),))
    if isinstance(spec, Sequence):
        return PathSet(tuple(Path(item) for item in spec))
    raise TypeError(f"Unsupported Dependencies input: {spec!r}")


@dataclass(frozen=True)
class Dependencies:
    """Files, values, and Python sources that affect a stage or resource.

    ``inputs`` maps names to paths (content-hashed unless wrapped with
    :meth:`stat`) or :meth:`glob` sets; ``values`` maps names to JSON values;
    ``sources`` lists Python files or directories whose normalized AST enters
    the stage's ``source_fp``.
    """

    inputs: Mapping[str, InputSpec] = field(default_factory=dict)
    values: Mapping[str, JSON] = field(default_factory=dict)
    sources: Sequence[Path | str] = ()

    @staticmethod
    def glob(
        root: Path | str,
        include: str | Sequence[str] = "**",
        exclude: str | Sequence[str] = (),
        allow_empty: bool = False,
    ) -> Glob:
        """Declare the files under ``root`` matching ``include`` minus ``exclude``.

        Matching happens when the dependency is fingerprinted; a missing root
        or (unless ``allow_empty``) an empty match is an error then.
        """

        return Glob(Path(root), _strs(include), _strs(exclude), allow_empty)

    @staticmethod
    def stat(paths: Path | str | Sequence[Path | str] | Glob) -> PathSet | Glob:
        """Fingerprint inputs by ``(relpath, size, mtime_ns)`` instead of content."""

        if isinstance(paths, Glob):
            return replace(paths, mode="stat")
        return replace(as_input_set(paths), mode="stat")


DependsSpec: TypeAlias = Dependencies | Callable[[DepCtx], Dependencies]


def resolve_dependencies(spec: DependsSpec | None, ctx: DepCtx) -> Dependencies:
    if spec is None:
        return Dependencies()
    result = spec(ctx) if callable(spec) else spec
    if not isinstance(result, Dependencies):
        raise TypeError(f"depends callable must return Dependencies, got {result!r}")
    return result


def merge_dependencies(*items: tuple[str, Dependencies]) -> Dependencies:
    """Merge labelled declarations, rejecting duplicate input/value names."""

    owners: dict[tuple[str, str], str] = {}

    def claim(kind: str, name: str, label: str) -> None:
        previous = owners.setdefault((kind, name), label)
        if previous != label:
            raise ValueError(
                f"Dependencies {kind} name {name!r} is declared by both {previous} and {label}"
            )

    inputs: dict[str, InputSpec] = {}
    values: dict[str, JSON] = {}
    sources: dict[Path | str, None] = {}
    for label, deps in items:
        for name, spec in deps.inputs.items():
            claim("inputs", name, label)
            inputs[name] = spec
        for name, value in deps.values.items():
            claim("values", name, label)
            values[name] = value
        sources.update(dict.fromkeys(deps.sources))
    return Dependencies(inputs=inputs, values=values, sources=tuple(sources))
