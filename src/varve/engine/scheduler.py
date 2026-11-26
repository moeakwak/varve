"""Sliding-window unit scheduling with ``exclusive`` and ``max_failures``.

A stage keeps up to ``workers`` units in flight and refills the window as
units complete (in completion order). An exclusive unit starts only when
nothing else of the stage is running and blocks every other unit while it
runs; the queue order is kept, so exclusive units never starve. Once a stop
is requested (first Ctrl-C, or ``max_failures`` reached) no new unit starts
and the active ones are awaited.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Literal, TypeVar

J = TypeVar("J")
R = TypeVar("R")

POLL_SECONDS = 0.25

HaltReason = Literal["interrupted", "max-failures", "broken-pool"]


class StopFlag:
    """Stop request shared by a run's stages.

    Lock-free (plain attribute writes), so a signal handler may call
    :meth:`request` whatever the interrupted code was holding.
    """

    def __init__(self) -> None:
        self._requested = False
        self.reason: HaltReason | None = None

    def request(self, reason: HaltReason = "interrupted") -> None:
        if self.reason is None:
            self.reason = reason
        self._requested = True

    @property
    def requested(self) -> bool:
        return self._requested


async def run_window(
    jobs: Sequence[J],
    submit: Callable[[J], Awaitable[R]],
    *,
    workers: int,
    is_exclusive: Callable[[J], bool],
    max_failures: int | None,
    stop: StopFlag,
    on_start: Callable[[J], None],
    on_done: Callable[[J, R | BaseException], bool],
    on_started: Callable[[], None] | None = None,
    on_tick: Callable[[], None] | None = None,
    should_halt: Callable[[], HaltReason | None] | None = None,
) -> HaltReason | None:
    """Run ``jobs`` through ``submit``; ``on_done`` returns whether the job failed.

    Returns why submissions stopped early (``None`` when every job ran).

    ``on_started`` runs after a batch of jobs was started and before any of
    them gets to run (the submitted coroutines only start at the next await).
    """

    pending = deque(jobs)
    active: dict[asyncio.Task[R], tuple[J, bool]] = {}
    failures = 0
    halted: HaltReason | None = None
    while pending or active:
        if halted is None and stop.requested:
            halted = "interrupted"
        started = False
        while halted is None and pending and len(active) < workers:
            if any(exclusive for _, exclusive in active.values()):
                break
            job = pending[0]
            exclusive = is_exclusive(job)
            if exclusive and active:
                break
            pending.popleft()
            on_start(job)
            active[asyncio.ensure_future(submit(job))] = (job, exclusive)
            started = True
            if exclusive:
                break
        if started and on_started is not None:
            on_started()
        if not active:
            break
        try:
            done, _ = await asyncio.wait(
                active, timeout=POLL_SECONDS, return_when=asyncio.FIRST_COMPLETED
            )
        except BaseException:
            _abandon(active)
            raise
        for task in done:
            job, _ = active.pop(task)
            try:
                result: R | BaseException = task.result()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                result = error
            if on_done(job, result):
                failures += 1
                if max_failures is not None and failures >= max_failures and halted is None:
                    halted = "max-failures"
        if halted is None and should_halt is not None:
            halted = should_halt()
        if on_tick is not None:
            on_tick()
    return halted


def _abandon(active: dict[asyncio.Task[Any], Any]) -> None:
    """Cancel unfinished tasks and retrieve finished ones' exceptions (abort path)."""

    for task in active:
        if not task.done():
            task.cancel()
        elif not task.cancelled():
            task.exception()
