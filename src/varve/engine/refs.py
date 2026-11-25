"""The seam through which cross-pipeline ``Ref`` inputs are resolved.

The engine never reads another pipeline's store itself. Whenever a stage (or
a dynamic-axis resolver) binds a ``Ref``, it asks the session's
:class:`RefResolver` for one :class:`RefBinding` per downstream coordinate.
The workspace layer provides the default implementation
(:class:`varve.workspace.refs.WorkspaceRefResolver`, zero-import reads of the
upstream ``state.db``); :class:`NoRefResolver` refuses every Ref.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from varve.axis import Axis
from varve.ref import Ref


class RefResolutionError(RuntimeError):
    """A Ref cannot be bound (no resolver, upstream missing, or not ``ok``)."""


@dataclass(frozen=True)
class RefBinding:
    """What one downstream coordinate receives from a Ref.

    ``value`` is injected (a ``Path``, or an ordered ``dict`` of paths for
    fan-in / multi-branch Refs). ``key`` enters the unit's ``upstream`` key
    segment and must be plain JSON (typically the upstream ``artifact_fp``,
    or ``[[key_id, artifact_fp], ...]`` for fan-in).
    """

    value: Path | dict[Any, Path]
    key: Any


@dataclass(frozen=True)
class RefInfo:
    """One upstream record set a bound Ref read (shown by ``status`` / ``inspect``)."""

    attr: str
    target: str
    stage: str
    branch: str
    root: Path
    units: int
    """Committed (``ok``) units of the upstream stage."""
    last_commit_at: str | None
    git_commit: str | None
    git_dirty: bool | None


class RefResolver(Protocol):
    def bind(
        self,
        name: str,
        ref: Ref,
        axes: Sequence[Axis],
        coords: Sequence[tuple[str, ...]],
    ) -> Sequence[RefBinding]:
        """Bind Ref ``name`` for each downstream coordinate.

        ``axes`` are the downstream stage's axes (or a resolver's static
        axes) and ``coords`` the canonical ids of each coordinate in that
        order; return exactly one binding per coordinate, or raise
        :class:`RefResolutionError`.
        """
        ...


def bound_refs(resolver: object) -> list[RefInfo]:
    """What ``resolver`` read so far, when it reports it (``bound_refs()``)."""

    report = getattr(resolver, "bound_refs", None)
    if not callable(report):
        return []
    return [info for info in cast("Iterable[object]", report()) if isinstance(info, RefInfo)]


class NoRefResolver:
    """A resolver that refuses every Ref (the workspace resolver is the default)."""

    def bind(
        self,
        name: str,
        ref: Ref,
        axes: Sequence[Axis],
        coords: Sequence[tuple[str, ...]],
    ) -> Sequence[RefBinding]:
        raise RefResolutionError(
            f"Ref {name!r} ({ref.describe()}, branch {ref.branch!r}) cannot be resolved: "
            "Ref resolution not available"
        )
