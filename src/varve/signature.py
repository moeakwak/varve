"""Parameter-name binding of stage methods and axis resolvers."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Collection
from dataclasses import dataclass
from typing import Any, Literal

ParamKind = Literal["ctx", "axis", "upstream", "ref", "resource"]


class BindingError(ValueError):
    """A stage or resolver parameter that cannot be bound by name."""


@dataclass(frozen=True)
class Param:
    name: str
    kind: ParamKind


@dataclass(frozen=True)
class Namespace:
    """Names a callable may bind.

    ``resources=None`` means the resources are only known per cell (a
    ``resources=`` callable): any otherwise unknown name is taken to be a
    resource and checked when the cell's resources are resolved.
    """

    axes: Collection[str]
    stages: Collection[str]
    refs: Collection[str]
    resources: Collection[str] | None = ()
    all_axes: Collection[str] = ()


def bind_params(
    func: Callable[..., Any],
    namespace: Namespace,
    *,
    owner: str,
    skip_self: bool,
) -> tuple[Param, ...]:
    """Classify every parameter of ``func`` by name, or raise ``BindingError``."""

    try:
        parameters = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError) as error:
        raise BindingError(f"{owner}: cannot inspect signature: {error}") from error
    if skip_self:
        if not parameters:
            raise BindingError(f"{owner}: a stage method must accept self")
        parameters = parameters[1:]
    bound: list[Param] = []
    for parameter in parameters:
        if parameter.kind in (parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD):
            raise BindingError(f"{owner}: *args/**kwargs cannot be injected")
        if parameter.kind is parameter.POSITIONAL_ONLY:
            raise BindingError(f"{owner}: positional-only parameter {parameter.name!r}")
        bound.append(Param(parameter.name, _classify(parameter.name, namespace, owner)))
    return tuple(bound)


def _classify(name: str, namespace: Namespace, owner: str) -> ParamKind:
    if name == "ctx":
        return "ctx"
    if name in namespace.axes:
        return "axis"
    if name in namespace.stages:
        return "upstream"
    if name in namespace.refs:
        return "ref"
    if name in namespace.all_axes:
        raise BindingError(f"{owner}: axis {name!r} is not available here (not in foreach)")
    if namespace.resources is None or name in namespace.resources:
        return "resource"
    candidates = sorted(
        {"ctx", *namespace.axes, *namespace.stages, *namespace.refs, *(namespace.resources or ())}
    )
    raise BindingError(f"{owner}: cannot inject parameter {name!r}; expected one of {candidates}")
