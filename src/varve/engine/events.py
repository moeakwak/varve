"""Run events, the in-process event bus, and the ``ctx`` sinks that feed it.

The scheduler and unit workers publish small immutable events; the TUI and
the plain logger subscribe. Process-pool workers cannot reach the bus, so
their sink puts events on a multiprocessing queue that a reader thread in
the main process republishes.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any, TypeVar

from varve.context import Ctx

T = TypeVar("T")

PROGRESS_INTERVAL = 0.2


@dataclass(frozen=True)
class RunStarted:
    pipeline: str
    branch: str
    run_id: str
    stages: tuple[str, ...]


@dataclass(frozen=True)
class StagePlanned:
    stage: str
    status: str
    """``ready``, ``awaiting``, ``blocked`` or ``skipped``."""
    total: int = 0
    hit: int = 0
    to_run: int = 0
    linked: int = 0
    workers: int = 0
    backend: str = ""
    detail: str | None = None


@dataclass(frozen=True)
class UnitStarted:
    stage: str
    unit_key: str
    at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class UnitProgress:
    stage: str
    unit_key: str
    done: int
    total: int | None
    desc: str | None


@dataclass(frozen=True)
class UnitFinished:
    stage: str
    unit_key: str
    ok: bool
    duration_ms: int
    error: str | None = None
    linked_from: str | None = None


@dataclass(frozen=True)
class StageFinished:
    stage: str
    status: str
    executed: int
    hit: int
    failed: int
    wall_ms: int
    error: str | None = None


@dataclass(frozen=True)
class RunFinished:
    status: str
    wall_ms: int


@dataclass(frozen=True)
class Notice:
    text: str
    level: str = "info"


Event = (
    RunStarted
    | StagePlanned
    | UnitStarted
    | UnitProgress
    | UnitFinished
    | StageFinished
    | RunFinished
    | Notice
)


class EventBus:
    """Thread-safe fan-out of events to subscribers (called synchronously)."""

    def __init__(self) -> None:
        self._subscribers: list[Callable[[Event], None]] = []
        self._lock = threading.Lock()

    def subscribe(self, callback: Callable[[Event], None]) -> None:
        self._subscribers.append(callback)

    def publish(self, event: Event) -> None:
        with self._lock:
            for callback in self._subscribers:
                callback(event)


def _report(iterable: Iterable[T], emit: Callable[[int], None]) -> Iterator[T]:
    """Yield ``iterable``, emitting the done count at start, end, and every interval."""

    last = 0.0
    done = 0
    emit(0)
    for item in iterable:
        yield item
        done += 1
        now = time.monotonic()
        if now - last >= PROGRESS_INTERVAL:
            last = now
            emit(done)
    emit(done)


class BusSink:
    """In-process sink: progress becomes bus events.

    With ``tqdm_fallback`` (no TUI, stderr is a terminal, output not
    captured) progress is also drawn with tqdm.
    """

    def __init__(self, bus: EventBus, *, tqdm_fallback: bool = False) -> None:
        self.bus = bus
        self.tqdm_fallback = tqdm_fallback

    def progress(
        self, ctx: Ctx[Any, Any], iterable: Iterable[T], total: int | None, desc: str | None
    ) -> Iterator[T]:
        def emit(done: int) -> None:
            self.bus.publish(UnitProgress(ctx.stage, ctx.unit_key, done, total, desc))

        if self.tqdm_fallback and sys.stderr.isatty():
            from tqdm import tqdm

            iterable = tqdm(iterable, total=total, desc=desc or ctx.unit_key or ctx.stage)
        return _report(iterable, emit)


class QueueSink:
    """Process-worker sink: events go to a multiprocessing queue (best effort)."""

    def __init__(self, queue: Any) -> None:
        self.queue = queue

    def _put(self, event: Event) -> None:
        try:
            self.queue.put_nowait(event)
        except Exception:  # a full or closed queue must never fail the unit
            pass

    def progress(
        self, ctx: Ctx[Any, Any], iterable: Iterable[T], total: int | None, desc: str | None
    ) -> Iterator[T]:
        def emit(done: int) -> None:
            self._put(UnitProgress(ctx.stage, ctx.unit_key, done, total, desc))

        return _report(iterable, emit)


def pump(queue: Any, bus: EventBus) -> threading.Thread:
    """Republish queue events on ``bus`` until a ``None`` sentinel arrives."""

    def run() -> None:
        while True:
            try:
                event = queue.get()
            except (EOFError, OSError):
                return
            if event is None:
                return
            bus.publish(event)

    thread = threading.Thread(target=run, name="varve-events", daemon=True)
    thread.start()
    return thread
