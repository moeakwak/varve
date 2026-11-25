"""Resource preflight and session lifetimes by scope.

Sessions are keyed by Resource *identity*: a stage whose cells use different
Resource objects with the same name (``resources=lambda ctx: [...]``) opens
one session per object, and each unit is injected the session of its own
cell's resource. Within a stage every distinct Resource gets an index
(:class:`ResourceTable`); jobs carry ``name -> index`` and sessions are
looked up by index, in the main process and in process-pool workers alike.

* ``preflight`` runs once per run per Resource object, in the main process,
  before the first stage that executes a unit needing it.
* ``scope="stage"``: opened in the main process when a stage has units to
  run, shared by its units, closed when the stage ends. The process backend
  pickles the session into its workers, so it must be picklable.
* ``scope="worker"``: opened once per process-pool worker (pool initializer);
  with in-process backends it behaves like ``"stage"``.
* ``scope="unit"``: opened around each unit, wherever the unit runs.

``preflight``/``open`` receive a :class:`RunCtx` whose ``config`` records
field reads. Those reads are merged into the ``config_access`` of every unit
using the resource (stage/worker scope: all units of the stage using it;
unit scope: that unit), so changing a Config field read only while opening
a resource invalidates its units.

Nothing is opened for stages whose units are all hits.
"""

from __future__ import annotations

import pickle
from collections.abc import Iterable, Mapping, Sequence
from contextlib import AsyncExitStack, ExitStack
from dataclasses import replace
from typing import Any

from varve.keying.config_access import ConfigAccess, merge_access, record_config
from varve.resource import Resource, RunCtx


class ResourceError(RuntimeError):
    """A resource failed its preflight, could not open, or cannot cross processes."""


class ResourceTable:
    """Distinct Resource objects of one stage run, indexed by first appearance."""

    def __init__(self) -> None:
        self.resources: list[Resource[Any]] = []
        self._index: dict[int, int] = {}

    def add(self, resource: Resource[Any]) -> int:
        index = self._index.get(id(resource))
        if index is None:
            index = self._index[id(resource)] = len(self.resources)
            self.resources.append(resource)
        return index

    def indices(self, resources: Iterable[Resource[Any]]) -> dict[str, int]:
        """``name -> index`` of one cell's resources (names are unique within a cell)."""

        return {resource.name: self.add(resource) for resource in resources}


def recording_ctx(run_ctx: RunCtx, access: ConfigAccess) -> RunCtx:
    """``run_ctx`` whose ``config`` records reads into ``access``."""

    return replace(run_ctx, config=record_config(run_ctx.config, access))


def resolve_accesses(accesses: Iterable[ConfigAccess]) -> list[str] | None:
    merged: list[str] | None = []
    for access in accesses:
        merged = merge_access(merged, access.resolve())
    return merged


class ResourceManager:
    def __init__(self) -> None:
        # id -> (resource, access); the resource is kept so its id is not reused.
        self._preflighted: dict[int, tuple[Resource[Any], ConfigAccess]] = {}

    def preflight(
        self, resources: Sequence[Resource[Any]], run_ctx: RunCtx
    ) -> dict[int, ConfigAccess]:
        """Run pending preflights; returns the Config reads of each preflight by table index."""

        accesses: dict[int, ConfigAccess] = {}
        for index, resource in enumerate(resources):
            if resource.preflight is None:
                continue
            done = self._preflighted.get(id(resource))
            if done is not None and done[0] is resource:
                accesses[index] = done[1]
                continue
            access = ConfigAccess()
            try:
                resource.preflight(recording_ctx(run_ctx, access))
            except Exception as error:
                raise ResourceError(
                    f"Resource {resource.name!r} preflight failed: {type(error).__name__}: {error}"
                ) from error
            self._preflighted[id(resource)] = (resource, access)
            accesses[index] = access
        return accesses

    async def open_shared(
        self,
        resources: Sequence[Resource[Any]],
        run_ctx: RunCtx,
        stack: AsyncExitStack,
        *,
        backend: str,
    ) -> tuple[dict[int, Any], dict[int, ConfigAccess]]:
        """Open stage-scope (and, in-process, worker-scope) sessions on ``stack``.

        Returns sessions and the Config reads of each ``open`` by table index.
        """

        sessions: dict[int, Any] = {}
        accesses: dict[int, ConfigAccess] = {}
        for index, resource in enumerate(resources):
            in_worker = resource.scope == "worker" and backend == "process"
            if resource.scope == "unit" or in_worker:
                continue
            if resource.open is None:
                sessions[index] = None
                continue
            access = ConfigAccess()
            try:
                manager = resource.open(recording_ctx(run_ctx, access))
                if hasattr(manager, "__aenter__"):
                    session = await stack.enter_async_context(manager)  # type: ignore[arg-type]
                else:
                    session = stack.enter_context(manager)  # type: ignore[arg-type]
            except Exception as error:
                raise ResourceError(
                    f"Resource {resource.name!r} failed to open: {type(error).__name__}: {error}"
                ) from error
            if backend == "process":
                check_picklable(resource, session)
            sessions[index] = session
            accesses[index] = access
        return sessions, accesses


def check_picklable(resource: Resource[Any], session: Any) -> None:
    try:
        pickle.dumps(session)
    except Exception as error:
        raise ResourceError(
            f"Resource {resource.name!r} (scope='stage') opened a {type(session).__name__} "
            f"session that cannot be pickled for backend='process' ({type(error).__name__}: "
            f"{error}). Return a picklable handle (for example an endpoint config) or use "
            "scope='worker' to open one session per worker process."
        ) from error


def open_worker_sessions(
    resources: Mapping[int, Resource[Any]], run_ctx: RunCtx, stack: ExitStack
) -> tuple[dict[int, Any], dict[int, ConfigAccess]]:
    """Open worker-scope sessions inside a process-pool worker (by table index)."""

    sessions: dict[int, Any] = {}
    accesses: dict[int, ConfigAccess] = {}
    for index, resource in resources.items():
        if resource.scope != "worker":
            continue
        if resource.open is None:
            sessions[index] = None
            continue
        access = ConfigAccess()
        manager = resource.open(recording_ctx(run_ctx, access))
        if not hasattr(manager, "__enter__"):
            raise ResourceError(
                f"Resource {resource.name!r} (scope='worker') returned an async context manager; "
                "process workers need a sync context manager"
            )
        sessions[index] = stack.enter_context(manager)  # type: ignore[arg-type]
        accesses[index] = access
    return sessions, accesses
