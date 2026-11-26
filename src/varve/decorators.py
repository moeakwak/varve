"""The ``@stage`` decorator and its normalized declaration."""

from __future__ import annotations

import inspect
import string
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any, Literal, TypeAlias, TypeVar

from varve.axis import Axis
from varve.dependencies import DepCtx, Dependencies, DependsSpec
from varve.ref import Ref
from varve.resource import Resource, RunCtx

F = TypeVar("F", bound=Callable[..., Any])
Backend = Literal["auto", "async", "process", "thread"]
NeedItem: TypeAlias = str | Callable[..., Any] | Ref
ResourcesSpec: TypeAlias = Sequence[Resource[Any]] | Callable[[DepCtx], Sequence[Resource[Any]]]
WorkersSpec: TypeAlias = int | Callable[[RunCtx], int]
Selector: TypeAlias = tuple[tuple[str, str], ...]

_GLOB_CHARS = frozenset("*?[")


def check_relpath(path: str, what: str) -> str:
    """Validate a relative POSIX path that must stay inside its base directory."""

    if not isinstance(path, str) or not path:
        raise ValueError(f"{what} must be a non-empty relative path, got {path!r}")
    parts = PurePosixPath(path).parts
    if path.startswith("/") or "\\" in path or ".." in parts or "\0" in path:
        raise ValueError(f"{what} must be a relative path inside the directory: {path!r}")
    return path


def is_glob(path: str) -> bool:
    return bool(_GLOB_CHARS.intersection(path))


def parse_selector(text: str) -> Selector:
    """Parse ``"axis=id"`` or ``"a=x,b=y"`` into ``(("a", "x"), ("b", "y"))``."""

    pairs: list[tuple[str, str]] = []
    for part in text.split(","):
        name, sep, item = part.strip().partition("=")
        if not sep or not name or not item:
            raise ValueError(f"Invalid coordinate selector {text!r}; expected 'axis=id[,axis=id]'")
        pairs.append((name.strip(), item.strip()))
    return tuple(pairs)


@dataclass(frozen=True)
class PublishEntry:
    """One ``publish=`` item: ``src`` inside the unit dir, ``dst`` under the publish dir.

    ``templated`` entries come from the mapping form and may use ``{axis}``
    placeholders; other entries keep their name and, for multi-unit stages,
    are placed under ``<unit_relpath>/``. A mapping target ending in ``/`` is
    a directory (``into_dir``): the source, or every file a glob source
    matches, is placed below it.
    """

    src: str
    dst: str
    templated: bool

    @property
    def into_dir(self) -> bool:
        return self.templated and self.dst.endswith("/")

    def placeholders(self) -> set[str]:
        return {name for _, name, _, _ in string.Formatter().parse(self.dst) if name is not None}


@dataclass(frozen=True)
class StageSpec:
    """Normalized ``@stage`` declaration. ``None`` fields fall back to ``Pipeline.defaults``."""

    name: str
    func: Callable[..., Any]
    foreach: tuple[Axis, ...]
    needs: tuple[str | Ref, ...]
    resources: ResourcesSpec | None
    depends: DependsSpec | None
    workers: WorkersSpec | None
    exclusive: tuple[Selector, ...]
    backend: Backend | None
    max_failures: int | None
    storage: str | None
    publish: tuple[PublishEntry, ...]
    produces: tuple[str, ...] | None
    produces_file: str | None
    version: str | None
    is_async: bool

    @property
    def produces_key(self) -> str | list[str] | None:
        """``produces=`` as it enters the key: the file name, a sorted list, or ``None``."""

        if self.produces_file is not None:
            return self.produces_file
        return sorted(self.produces) if self.produces else None


def _as_tuple(value: Any, kinds: type | tuple[type, ...]) -> tuple[Any, ...]:
    if value is None:
        return ()
    if isinstance(value, kinds):
        return (value,)
    return tuple(value)


def _need(item: NeedItem) -> str | Ref:
    if isinstance(item, str | Ref):
        return item
    spec = getattr(item, "__varve_stage__", None)
    if spec is not None:
        return spec.name
    name = getattr(item, "__name__", None)
    if callable(item) and isinstance(name, str):
        return name
    raise TypeError(f"needs= accepts stage names, stage methods, or Ref objects, got {item!r}")


def _publish(
    publish: bool | str | Sequence[str] | Mapping[str, str] | None,
    produces: tuple[str, ...] | None,
    stage_name: str,
) -> tuple[PublishEntry, ...]:
    if publish is None or publish is False:
        return ()
    if publish is True:
        if not produces:
            raise ValueError(f"Stage {stage_name!r}: publish=True requires produces=")
        return tuple(PublishEntry(path, path, False) for path in produces)
    if isinstance(publish, Mapping):
        entries = []
        for src, dst in publish.items():
            check_relpath(src, f"Stage {stage_name!r} publish source")
            check_relpath(dst, f"Stage {stage_name!r} publish target")
            if is_glob(src) and not dst.endswith("/"):
                raise ValueError(
                    f"Stage {stage_name!r}: a glob publish source needs a directory target "
                    f"ending in '/': {src!r}"
                )
            entries.append(PublishEntry(src, dst, True))
        return tuple(entries)
    items = _as_tuple(publish, str)
    for item in items:
        check_relpath(item, f"Stage {stage_name!r} publish path")
    return tuple(PublishEntry(item, item, False) for item in items)


def is_positive_int(value: Any) -> bool:
    """A positive ``int`` that is not a ``bool``."""

    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def stage(
    *,
    foreach: Axis | tuple[Axis, ...] | None = None,
    needs: NeedItem | Sequence[NeedItem] | None = None,
    resources: ResourcesSpec | None = None,
    depends: DependsSpec | None = None,
    workers: WorkersSpec | None = None,
    exclusive: str | Sequence[str] | None = None,
    backend: Backend | None = None,
    max_failures: int | None = None,
    storage: str | None = None,
    publish: bool | str | Sequence[str] | Mapping[str, str] | None = None,
    produces: str | Sequence[str] | None = None,
    version: int | str | None = None,
) -> Callable[[F], F]:
    """Declare a pipeline stage.

    Parameters of the decorated method are injected by name: ``ctx``, axis
    values, upstream stages / ``Ref`` attributes, and resources. The method
    must return ``None`` and write its managed output under ``ctx.out``.
    ``workers``, ``backend``, ``storage``, ``max_failures`` left as ``None``
    fall back to ``Pipeline.defaults`` (then ``1``, ``"auto"``, ``"default"``,
    no limit); ``resources`` and ``depends`` are merged with the defaults.
    """

    axes = _as_tuple(foreach, Axis)
    for axis in axes:
        if not isinstance(axis, Axis):
            raise TypeError(f"foreach= accepts Axis objects, got {axis!r}")
    if len({id(axis) for axis in axes}) != len(axes):
        raise ValueError("foreach= must not repeat an axis")
    if workers is not None and not callable(workers) and not is_positive_int(workers):
        raise ValueError(f"workers= must be a positive int or a callable, got {workers!r}")
    if max_failures is not None and not is_positive_int(max_failures):
        raise ValueError(f"max_failures= must be a positive int, got {max_failures!r}")
    if backend is not None and backend not in ("auto", "async", "process", "thread"):
        raise ValueError(f"backend= must be auto, async, process, or thread, got {backend!r}")
    if resources is not None and not callable(resources):
        for resource in resources:
            if not isinstance(resource, Resource):
                raise TypeError(f"resources= accepts Resource objects, got {resource!r}")
    if depends is not None and not callable(depends) and not isinstance(depends, Dependencies):
        raise TypeError(f"depends= accepts Dependencies or a callable, got {depends!r}")
    produce_paths = None if produces is None else _as_tuple(produces, str)
    exclusive_rules = tuple(parse_selector(text) for text in _as_tuple(exclusive, str))
    if needs is None:
        need_items: tuple[str | Ref, ...] = ()
    elif isinstance(needs, str | Ref) or callable(needs):
        need_items = (_need(needs),)
    else:
        need_items = tuple(_need(item) for item in needs)

    def decorate(func: F) -> F:
        name = func.__name__
        if produce_paths is not None:
            if not produce_paths:
                raise ValueError(f"Stage {name!r}: produces= must not be empty")
            for path in produce_paths:
                check_relpath(path, f"Stage {name!r} produces")
        is_async = inspect.iscoroutinefunction(func)
        if inspect.isgeneratorfunction(func) or inspect.isasyncgenfunction(func):
            raise TypeError(f"Stage {name!r} must not be a generator; return None")
        if backend == "async" and not is_async:
            raise ValueError(f"Stage {name!r}: backend='async' requires an async def stage")
        if backend in ("process", "thread") and is_async:
            raise ValueError(f"Stage {name!r}: backend={backend!r} requires a plain def stage")
        spec = StageSpec(
            name=name,
            func=func,
            foreach=axes,
            needs=need_items,
            resources=resources,
            depends=depends,
            workers=workers,
            exclusive=exclusive_rules,
            backend=backend,
            max_failures=max_failures,
            storage=storage,
            publish=_publish(publish, produce_paths, name),
            produces=produce_paths,
            produces_file=produces if isinstance(produces, str) else None,
            version=None if version is None else str(version),
            is_async=is_async,
        )
        setattr(func, "__varve_stage__", spec)
        return func

    return decorate
