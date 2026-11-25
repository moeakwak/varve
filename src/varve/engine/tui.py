"""Run displays: a ``rich.Live`` TUI and a plain structured logger.

Both are read-only event-bus subscribers. The TUI shows the pipeline and
branch with overall progress, one line per stage (``hit`` / ``linked`` /
``running 142/500`` / ``awaiting`` / ``failed``), the active units with rate
and ETA, and the last failures.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import IO

from rich.console import Console, Group
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.table import Table
from rich.text import Text

from varve.engine.events import (
    Event,
    Notice,
    RunFinished,
    RunStarted,
    StageFinished,
    StagePlanned,
    UnitFinished,
    UnitProgress,
    UnitStarted,
)
from varve.style import _THEME, STATUS_STYLES, format_ms

MAX_ACTIVE_ROWS = 8
MAX_FAILURES = 3

logger = logging.getLogger("varve")


class PlainReporter:
    """Logs run events as single lines on the ``varve`` logger."""

    def __init__(self, log: logging.Logger = logger) -> None:
        self.log = log

    def __call__(self, event: Event) -> None:
        log = self.log
        if isinstance(event, RunStarted):
            log.info("▸ run %s --branch %s (%s)", event.pipeline, event.branch, event.run_id)
        elif isinstance(event, StagePlanned):
            if event.status != "ready":
                return
            if event.to_run == 0:
                linked = f", {event.linked} linked" if event.linked else ""
                log.info("[%s] ✓ hit (%d%s)", event.stage, event.total, linked)
            else:
                log.info(
                    "[%s] run %d of %d units (%d hit, %d linked) · workers=%d backend=%s",
                    event.stage,
                    event.to_run,
                    event.total,
                    event.hit,
                    event.linked,
                    event.workers,
                    event.backend,
                )
        elif isinstance(event, UnitFinished):
            if not event.ok:
                log.warning("[%s] failed %s: %s", event.stage, event.unit_key or "-", event.error)
            elif event.linked_from:
                log.debug("[%s] linked %s from %s", event.stage, event.unit_key, event.linked_from)
            else:
                log.debug(
                    "[%s] done %s · %s",
                    event.stage,
                    event.unit_key or "-",
                    format_ms(event.duration_ms),
                )
        elif isinstance(event, StageFinished):
            if event.status == "ok" and event.executed == 0:
                return
            detail = f" · {event.error}" if event.error else ""
            level = logging.INFO if event.status in ("ok", "partial") else logging.WARNING
            word = "done" if event.status == "ok" else event.status
            log.log(
                level,
                "[%s] %s · %d executed, %d failed · %s%s",
                event.stage,
                word,
                event.executed,
                event.failed,
                format_ms(event.wall_ms),
                detail,
            )
        elif isinstance(event, Notice):
            log.log(logging.WARNING if event.level == "warning" else logging.INFO, "%s", event.text)
        elif isinstance(event, RunFinished):
            level = logging.INFO if event.status == "ok" else logging.WARNING
            log.log(level, "run %s · %s", event.status, format_ms(event.wall_ms))


@dataclass
class _StageLine:
    name: str
    status: str = "pending"
    total: int = 0
    hit: int = 0
    linked: int = 0
    to_run: int = 0
    done: int = 0
    failed: int = 0
    detail: str | None = None
    started: float | None = None
    wall_ms: int | None = None


@dataclass
class _ActiveUnit:
    stage: str
    unit_key: str
    started: float
    done: int = 0
    total: int | None = None
    desc: str | None = None


@dataclass
class LiveState:
    pipeline: str = ""
    branch: str = ""
    stages: dict[str, _StageLine] = field(default_factory=dict)
    active: dict[tuple[str, str], _ActiveUnit] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    started: float = field(default_factory=time.monotonic)
    finished: str | None = None

    def apply(self, event: Event) -> None:
        if isinstance(event, RunStarted):
            self.pipeline, self.branch = event.pipeline, event.branch
            for name in event.stages:
                self.stages[name] = _StageLine(name)
        elif isinstance(event, StagePlanned):
            line = self.stages.setdefault(event.stage, _StageLine(event.stage))
            line.status = "running" if event.status == "ready" and event.to_run else event.status
            if event.status == "ready" and not event.to_run:
                line.status = "hit"
            line.total, line.hit, line.linked, line.to_run = (
                event.total,
                event.hit,
                event.linked,
                event.to_run,
            )
            line.detail = event.detail
            line.started = time.monotonic()
        elif isinstance(event, UnitStarted):
            self.active[(event.stage, event.unit_key)] = _ActiveUnit(
                event.stage, event.unit_key, event.at
            )
        elif isinstance(event, UnitProgress):
            unit = self.active.get((event.stage, event.unit_key))
            if unit is not None:
                unit.done, unit.total, unit.desc = event.done, event.total, event.desc
        elif isinstance(event, UnitFinished):
            self.active.pop((event.stage, event.unit_key), None)
            line = self.stages.get(event.stage)
            if line is not None and not event.linked_from:
                line.done += 1
                if not event.ok:
                    line.failed += 1
            if not event.ok:
                self.failures.append(f"{event.stage}@{event.unit_key or '-'}: {event.error}")
                del self.failures[:-MAX_FAILURES]
        elif isinstance(event, StageFinished):
            line = self.stages.setdefault(event.stage, _StageLine(event.stage))
            line.wall_ms = event.wall_ms
            if event.status == "ok":
                line.status = (
                    "linked"
                    if line.linked and not line.to_run
                    else ("hit" if not line.to_run else "done")
                )
            else:
                line.status = event.status
            line.detail = event.error or line.detail
        elif isinstance(event, Notice):
            self.notices.append(event.text)
            del self.notices[:-2]
        elif isinstance(event, RunFinished):
            self.finished = event.status

    # -- rendering -----------------------------------------------------------

    def render(self) -> Group:
        now = time.monotonic()
        to_run = sum(line.to_run for line in self.stages.values())
        done = sum(line.done for line in self.stages.values())
        header = Text()
        header.append("varve ", style="bold")
        header.append(self.pipeline, style="blue")
        header.append(f" @ {self.branch}", style="dim")
        header.append(f"   {done}/{to_run} units", style="bold")
        header.append(f"   {format_ms((now - self.started) * 1000)}", style="dim")
        if self.finished:
            header.append(f"   {self.finished}", style=STATUS_STYLES.get(self.finished, ""))

        stages = Table.grid(padding=(0, 2))
        stages.add_column(no_wrap=True)
        stages.add_column(no_wrap=True)
        stages.add_column(no_wrap=True)
        stages.add_column(overflow="ellipsis")
        for line in self.stages.values():
            bar: ProgressBar | Text = Text("")
            label = line.status
            if line.status == "running" and line.to_run:
                bar = ProgressBar(total=line.to_run, completed=line.done, width=20)
                label = f"running {line.done}/{line.to_run}"
            elif line.status == "hit":
                label = f"hit {line.total}" if line.total != 1 else "hit"
            info = []
            if line.linked:
                info.append(f"{line.linked} linked")
            if line.failed:
                info.append(f"{line.failed} failed")
            if line.wall_ms is not None and line.to_run:
                info.append(format_ms(line.wall_ms))
            if line.detail:
                info.append(line.detail)
            stages.add_row(
                Text(line.name, style="bold"),
                Text(label, style=STATUS_STYLES.get(line.status, "")),
                bar,
                Text(" · ".join(info), style="dim"),
            )

        parts: list[Text | Table] = [header, stages]
        if self.active:
            active = Table.grid(padding=(0, 2))
            for column in range(4):
                active.add_column(no_wrap=True)
            for unit in list(self.active.values())[:MAX_ACTIVE_ROWS]:
                elapsed = now - unit.started
                if unit.total:
                    rate = unit.done / elapsed if elapsed > 0 else 0.0
                    eta = (unit.total - unit.done) / rate if rate > 0 else None
                    progress = f"{unit.done}/{unit.total}"
                    speed = f"{rate:.1f}/s" + (f" · ETA {format_ms(eta * 1000)}" if eta else "")
                else:
                    progress = str(unit.done) if unit.done else ""
                    speed = ""
                active.add_row(
                    Text(f"{unit.stage}@{unit.unit_key or '-'}", style="cyan"),
                    Text(progress),
                    Text(speed, style="dim"),
                    Text(format_ms(elapsed * 1000), style="dim"),
                )
            parts += [Text("active", style="dim"), active]
        for failure in self.failures:
            parts.append(Text(f"✕ {failure}", style="red", overflow="ellipsis", no_wrap=True))
        for notice in self.notices:
            parts.append(Text(notice, style="yellow"))
        return Group(*parts)


class LiveDisplay:
    """Subscribes to the bus and redraws a ``rich.Live`` view on ``terminal``."""

    def __init__(self, terminal: IO[str]) -> None:
        self.state = LiveState()
        self._lock = threading.Lock()
        self.console = Console(file=terminal, theme=_THEME, highlight=False, force_terminal=True)
        self.live = Live(
            get_renderable=self._render,
            console=self.console,
            refresh_per_second=4,
            redirect_stdout=False,
            redirect_stderr=False,
            transient=False,
        )

    def __call__(self, event: Event) -> None:
        with self._lock:
            self.state.apply(event)

    def _render(self) -> Group:
        with self._lock:
            return self.state.render()

    def __enter__(self) -> LiveDisplay:
        self.live.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.live.stop()
