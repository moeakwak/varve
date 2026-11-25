"""Unit execution backends: inline, thread, async, and a ``spawn`` process pool.

``backend="auto"`` resolves to ``async`` for ``async def`` stages, ``inline``
(main thread) for ``def`` stages with one worker, and ``process`` otherwise.
"""

from __future__ import annotations

import asyncio
import contextvars
import importlib
import importlib.util
import io
import multiprocessing
import os
import pickle
import signal
import sys
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from multiprocessing.reduction import ForkingPickler
from multiprocessing.util import Finalize
from pathlib import Path
from typing import Any, Protocol

from varve.axis import Cell
from varve.context import NullSink
from varve.dependencies import DepCtx
from varve.engine.events import QueueSink
from varve.engine.resources import open_worker_sessions
from varve.engine.unit import UnitJob, UnitOutcome, WorkerState, execute, execute_async
from varve.graph import StageNode
from varve.keying.config_access import ConfigAccess
from varve.pipeline import Pipeline
from varve.resource import Resource, RunCtx


def resolve_backend(node: StageNode, workers: int) -> str:
    if node.backend != "auto":
        return node.backend
    if node.spec.is_async:
        return "async"
    return "inline" if workers == 1 else "process"


class Backend(Protocol):
    name: str

    async def submit(self, job: UnitJob) -> UnitOutcome: ...

    def close(self, *, abort: bool = False) -> None: ...


class InlineBackend:
    """Runs each unit in the main thread (blocks the event loop while it runs)."""

    name = "inline"

    def __init__(self, state: WorkerState) -> None:
        self.state = state

    async def submit(self, job: UnitJob) -> UnitOutcome:
        return execute(self.state, job)

    def close(self, *, abort: bool = False) -> None:
        return None


class ThreadBackend:
    name = "thread"

    def __init__(self, state: WorkerState, workers: int) -> None:
        self.state = state
        self.pool = ThreadPoolExecutor(workers, thread_name_prefix="varve-unit")

    async def submit(self, job: UnitJob) -> UnitOutcome:
        loop = asyncio.get_running_loop()
        context = contextvars.copy_context()
        state = self.state

        def call() -> UnitOutcome:
            return context.run(execute, state, job)

        return await loop.run_in_executor(self.pool, call)

    def close(self, *, abort: bool = False) -> None:
        self.pool.shutdown(wait=not abort, cancel_futures=True)


class AsyncBackend:
    name = "async"

    def __init__(self, state: WorkerState) -> None:
        self.state = state

    async def submit(self, job: UnitJob) -> UnitOutcome:
        return await execute_async(self.state, job)

    def close(self, *, abort: bool = False) -> None:
        return None


@dataclass(frozen=True)
class PipelineLocator:
    """How a spawned worker imports the pipeline class."""

    module: str
    qualname: str
    file: str | None

    @classmethod
    def of(cls, pipeline: type[Pipeline]) -> PipelineLocator:
        module = pipeline.import_module_name()
        file = str(pipeline.module_file()) if module == "__main__" else None
        return cls(module, pipeline.__qualname__, file)

    def load(self) -> type[Pipeline]:
        if "<locals>" in self.qualname:
            raise TypeError(
                f"Pipeline {self.qualname} is defined inside a function; the process backend "
                "needs a module-level class"
            )
        if self.module != "__main__":
            return _lookup(importlib.import_module(self.module), self.qualname)
        main = sys.modules.get("__main__")
        try:
            return _lookup(main, self.qualname)
        except AttributeError:
            if self.file is None:
                raise
        spec = importlib.util.spec_from_file_location("__varve_main__", self.file)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules["__varve_main__"] = module
        spec.loader.exec_module(module)
        return _lookup(module, self.qualname)


def _lookup(module: Any, qualname: str) -> type[Pipeline]:
    target = module
    for part in qualname.split("."):
        target = getattr(target, part)
    return target


def _main_module_name() -> str | None:
    """Import name of this process' ``__main__`` when spawn will not re-run it in workers.

    ``spawn`` re-runs a script (``python file.py``) or a plain module (``python -m pkg.mod``)
    as the worker's ``__main__``, but skips a package's ``__main__`` (``python -m pkg``);
    objects pickled as ``__main__.X`` then have to be looked up in ``pkg.__main__``.
    """

    name = getattr(getattr(sys.modules.get("__main__"), "__spec__", None), "name", None)
    return name if isinstance(name, str) and name.endswith(".__main__") else None


def _dumps(obj: Any) -> bytes:
    return bytes(ForkingPickler.dumps(obj))


class _WorkerUnpickler(pickle.Unpickler):
    """Resolves ``__main__.X`` references that the worker's ``__main__`` does not define.

    Fallbacks: the pipeline file loaded by :class:`PipelineLocator` (``__varve_main__``) and
    the parent's package ``__main__`` imported under its real name (``pkg.__main__``), so
    Config/Args instances, ``__main__`` Enums in axis values, picklable sessions, etc.
    resolve to the same classes as the located pipeline uses.
    """

    main_module: str | None = None

    def find_class(self, module: str, name: str) -> Any:
        if module != "__main__":
            return super().find_class(module, name)
        candidates = [sys.modules.get("__main__"), sys.modules.get("__varve_main__")]
        for candidate in candidates:
            if candidate is None:
                continue
            try:
                return _lookup(candidate, name)
            except AttributeError:
                continue
        if self.main_module is not None:
            return _lookup(importlib.import_module(self.main_module), name)
        return super().find_class(module, name)


def _loads(data: bytes) -> Any:
    return _WorkerUnpickler(io.BytesIO(data)).load()


@dataclass(frozen=True)
class WorkerSetup:
    pipeline: PipelineLocator
    stage: str
    config: Any
    args: Any
    branch: str
    root: Path
    shared: dict[int, Any]
    """Picklable stage-scope sessions opened by the main process, by table index."""
    shared_access: dict[int, list[list[str] | None]]
    """Resolved Config reads of the main process' preflight/open, by table index."""
    cells: tuple[tuple[Cell, tuple[int, ...]], ...]
    """Distinct cells of the units to run with the table index of each of their resources
    (in ``resources_for`` order); worker-scope resources are opened for all."""
    env: dict[str, str]
    queue: Any


_STATE: WorkerState | None = None


def _init_worker(
    locator: PipelineLocator,
    main_module: str | None,
    env: dict[str, str],
    payload: bytes,
    queue: Any,
) -> None:
    """Locate the pipeline class first, then unpickle the setup against it.

    The setup (and every job) travels as bytes so ``__main__`` references are resolved by
    :class:`_WorkerUnpickler` after the pipeline's module has been imported.
    """

    global _STATE
    signal.signal(signal.SIGINT, signal.SIG_IGN)  # the parent owns Ctrl-C handling
    if hasattr(signal, "pthread_sigmask"):
        # The parent blocks SIGINT while spawning workers; a Ctrl-C that arrived
        # during bootstrap is pending here and discarded now that it is ignored.
        signal.pthread_sigmask(signal.SIG_UNBLOCK, {signal.SIGINT})
    _WorkerUnpickler.main_module = main_module
    os.environ.update(env)
    pipeline = locator.load()
    setup: WorkerSetup = _loads(payload)
    node = pipeline.graph().nodes[setup.stage]
    resources: dict[int, Resource[Any]] = {}
    for cell, indices in setup.cells:
        found = node.resources_for(DepCtx(setup.config, cell))
        if len(found) != len(indices):
            raise RuntimeError(
                f"Stage {setup.stage!r}: resources of cell {dict(cell)} differ between the "
                "main process and a worker; resources= must be deterministic"
            )
        for resource, index in zip(found, indices, strict=True):
            resources.setdefault(index, resource)
    stack = ExitStack()
    Finalize(None, stack.close, exitpriority=10)
    run_ctx = RunCtx(setup.config, setup.args, setup.branch, setup.stage, setup.root)
    sessions = dict(setup.shared)
    opened, opened_access = open_worker_sessions(resources, run_ctx, stack)
    sessions.update(opened)
    resource_access = {
        index: [ConfigAccess.of(item) for item in items]
        for index, items in setup.shared_access.items()
    }
    for index, access in opened_access.items():
        resource_access.setdefault(index, []).append(access)
    _STATE = WorkerState(
        instance=pipeline(),
        node=node,
        config=setup.config,
        args=setup.args,
        branch=setup.branch,
        root=setup.root,
        sessions=sessions,
        sink=QueueSink(queue) if queue is not None else NullSink(),
        process_worker=True,
        resource_access=resource_access,
    )


def _run_job(payload: bytes) -> UnitOutcome:
    assert _STATE is not None, "worker was not initialized"
    return execute(_STATE, _loads(payload))


@contextmanager
def _sigint_blocked() -> Iterator[None]:
    """Block SIGINT in this thread so processes spawned meanwhile start with it blocked.

    A Ctrl-C arriving meanwhile stays pending in the parent and is delivered when
    the mask is restored; spawned workers inherit the mask and discard it once
    their initializer has set ``SIG_IGN``.
    """

    if not hasattr(signal, "pthread_sigmask"):
        yield
        return
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
    try:
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)


@contextmanager
def _environ(env: Mapping[str, str]) -> Iterator[None]:
    """Set ``env`` in ``os.environ`` for the block, then restore the previous values."""

    saved = {name: os.environ.get(name) for name in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


class ProcessBackend:
    """A ``spawn`` process pool.

    ``ProcessPoolExecutor`` spawns workers lazily on ``submit``, so pool creation
    and every submit run with ``[defaults] worker_env`` applied to the parent's
    ``os.environ`` (workers inherit it from interpreter start, before user
    modules are imported) and with SIGINT blocked (workers ignore Ctrl-C from
    birth; the parent owns interrupt handling).
    """

    name = "process"

    def __init__(self, setup: WorkerSetup, workers: int) -> None:
        self.env = dict(setup.env)
        # The spawn queue can only be pickled while spawning, so it stays an init arg.
        payload = _dumps(replace(setup, queue=None))
        with self._spawning():
            self.pool = ProcessPoolExecutor(
                workers,
                mp_context=multiprocessing.get_context("spawn"),
                initializer=_init_worker,
                initargs=(setup.pipeline, _main_module_name(), self.env, payload, setup.queue),
            )

    @contextmanager
    def _spawning(self) -> Iterator[None]:
        with _environ(self.env), _sigint_blocked():
            yield

    async def submit(self, job: UnitJob) -> UnitOutcome:
        with self._spawning():
            future = self.pool.submit(_run_job, _dumps(job))
        return await asyncio.wrap_future(future)

    def close(self, *, abort: bool = False) -> None:
        if abort:
            terminate = getattr(self.pool, "terminate_workers", None)
            if terminate is not None:
                terminate()
            else:
                for process in list(getattr(self.pool, "_processes", {}).values()):
                    process.terminate()
        self.pool.shutdown(wait=not abort, cancel_futures=True)


def distinct_cells(
    cells: Sequence[tuple[Cell, tuple[int, ...]]],
) -> tuple[tuple[Cell, tuple[int, ...]], ...]:
    seen: dict[tuple[tuple[str, str], ...], tuple[Cell, tuple[int, ...]]] = {}
    for cell, indices in cells:
        seen.setdefault(tuple(sorted((k, repr(v)) for k, v in cell.items())), (cell, indices))
    return tuple(seen.values())
