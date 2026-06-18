"""Stage decorators and their captured metadata."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from varve.keyspec import KeySpec

StageKind = Literal["single", "batch"]
ProducesSpec = str | list[str] | Callable[[Any], list[str]] | None


@dataclass(frozen=True)
class StageSpec:
    name: str
    kind: StageKind
    func: Callable[..., Any]
    needs: tuple[str, ...]
    produces: ProducesSpec
    keyspec: KeySpec
    uses: tuple[Callable[..., Any], ...]
    partition_key: tuple[str, ...] = ()


def _normalize_needs(needs: str | list[str] | tuple[str, ...] | None) -> tuple[str, ...]:
    if needs is None:
        return ()
    if isinstance(needs, str):
        return (needs,)
    return tuple(needs)


def _attach_stage_spec(func: Callable[..., Any], spec: StageSpec) -> Callable[..., Any]:
    setattr(func, "__varve_stage__", spec)
    return func


def stage(
    *,
    needs: str | list[str] | tuple[str, ...] | None = None,
    produces: ProducesSpec = None,
    key: list[str] | tuple[str, ...] | KeySpec | None = None,
    uses: list[Callable[..., Any]] | tuple[Callable[..., Any], ...] = (),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare a single-run stage."""

    def decorate(func: Callable[..., Any]) -> Callable[..., Any]:
        spec = StageSpec(
            name=func.__name__,
            kind="single",
            func=func,
            needs=_normalize_needs(needs),
            produces=produces,
            keyspec=KeySpec.coerce(key),
            uses=tuple(uses),
        )
        return _attach_stage_spec(func, spec)

    return decorate


def batch_stage(
    *,
    needs: str | list[str] | tuple[str, ...] | None = None,
    produces: ProducesSpec = None,
    key: list[str] | tuple[str, ...] | KeySpec | None = None,
    uses: list[Callable[..., Any]] | tuple[Callable[..., Any], ...] = (),
    partition_key: list[str] | tuple[str, ...] = (),
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Declare an async-generator batch stage."""

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
            keyspec=KeySpec.coerce(key),
            uses=tuple(uses),
            partition_key=tuple(partition_key),
        )
        return _attach_stage_spec(func, spec)

    return decorate

