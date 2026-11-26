"""``run``: stage-ordered execution with startup recovery and run bookkeeping.

Stages run in topological order behind a barrier: every unit of a stage
finishes (and is committed) before its dependents are expanded and keyed, so
dynamic axes that read an upstream stage resolve right after it completes.
A failed stage skips its dependents; independent stages still run.
"""

from __future__ import annotations

import asyncio
import shutil
import signal
import socket
import sqlite3
import sys
import threading
import time
import traceback
from collections.abc import Iterator, Sequence
from concurrent.futures.process import BrokenProcessPool
from contextlib import AsyncExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from varve.axis import Cell
from varve.context import CtxSink
from varve.engine.backends import (
    AsyncBackend,
    Backend,
    InlineBackend,
    PipelineLocator,
    ProcessBackend,
    ThreadBackend,
    WorkerSetup,
    distinct_cells,
    resolve_backend,
)
from varve.engine.capture import routed_std_streams
from varve.engine.commit import Committer
from varve.engine.events import (
    BusSink,
    EventBus,
    Notice,
    RunFinished,
    RunStarted,
    StageFinished,
    StagePlanned,
    UnitFinished,
    UnitStarted,
    pump,
)
from varve.engine.housekeeping import git_state, new_run_id, percentile, recover, rotate_logs
from varve.engine.probe import (
    ProbeError,
    StageEvaluator,
    StagePlan,
    StageState,
    UnitPlan,
    probe_state,
)
from varve.engine.publish import PublishError, publish_stage
from varve.engine.refs import RefResolutionError
from varve.engine.resources import ResourceError, ResourceManager, ResourceTable
from varve.engine.scheduler import StopFlag, run_window
from varve.engine.session import Session, identity_meta
from varve.engine.unit import (
    UnitJob,
    UnitOutcome,
    WorkerState,
    peak_rss_mb,
    remove_tree,
)
from varve.engine.writer import Writer
from varve.graph import GraphError, StageNode, check_publish_collisions
from varve.keying.config_access import ConfigAccess
from varve.keying.keys import InputKey
from varve.resource import RunCtx
from varve.store import layout
from varve.store.db import now_iso
from varve.store.lock import branch_lock, pipeline_lock
from varve.store.repo import BranchRow, RunRow, StageRunRow

StageStatus = Literal["ok", "failed", "interrupted", "partial", "blocked", "skipped"]
RunStatus = Literal["ok", "failed", "interrupted"]


@dataclass(frozen=True)
class RunOptions:
    """Selection and behavior of one run (branch and Args are passed separately)."""

    only: str | None = None
    upto: str | None = None
    downstream: str | None = None
    force: bool = False
    rehash: bool = False
    failed_only: bool = False
    tui: bool | None = None
    """``None``: on when stdout is a terminal."""
    argv: Sequence[str] | None = None
    """Recorded in ``runs.cli_argv_json`` (default: ``sys.argv``)."""


@dataclass
class StageOutcome:
    name: str
    status: StageStatus
    total: int = 0
    hit: int = 0
    linked: int = 0
    executed: int = 0
    failed: int = 0
    orphaned: int = 0
    wall_ms: int = 0
    detail: str | None = None


@dataclass
class RunResult:
    run_id: str
    branch: str
    status: RunStatus
    stages: dict[str, StageOutcome] = field(default_factory=dict)
    wall_ms: int = 0

    @property
    def exit_code(self) -> int:
        return {"ok": 0, "failed": 1, "interrupted": 130}[self.status]


class StorageRootError(ValueError):
    """A storage root differs from the one recorded in the store."""


def check_storage_roots(recorded: dict[str, Path], current: dict[str, Path]) -> None:
    """Reject storage roots that moved since the store recorded them.

    The ``storages`` table is per pipeline, so roots must not depend on the
    branch Config: a second branch with another root would redirect every
    branch's recorded artifact paths.
    """

    for name, root in current.items():
        previous = recorded.get(name)
        if previous is not None and Path(previous) != Path(root):
            raise StorageRootError(
                f"storage root for {name!r} changed from {previous} to {root}; storage roots "
                "must not depend on branch config, or clean the pipeline first"
            )


def select_stages(
    session: Session, *, only: str | None, upto: str | None, downstream: str | None
) -> list[str]:
    graph = session.graph
    for name in (only, upto, downstream):
        if name is not None and name not in graph.nodes:
            raise ValueError(f"Unknown stage {name!r}; stages: {list(graph.order)}")
    if only is not None:
        return [only]
    if upto is not None:
        return [*graph.ancestors(upto), upto]
    if downstream is not None:
        return [downstream, *graph.descendants(downstream)]
    return list(graph.order)


class Runner:
    def __init__(self, session: Session, options: RunOptions, bus: EventBus) -> None:
        self.session = session
        self.options = options
        self.bus = bus
        self.run_id = new_run_id()
        self.stop = StopFlag()
        self.tui_active = False
        self.selected = select_stages(
            session, only=options.only, upto=options.upto, downstream=options.downstream
        )
        needed = set(self.selected)
        for name in self.selected:
            needed.update(session.graph.ancestors(name))
        self.needed = [name for name in session.graph.order if name in needed]
        self.outcomes: dict[str, StageOutcome] = {}
        self.states: dict[str, StageState] = {}
        self.publish_claims: dict[tuple[str, int], dict[str, str]] = {}
        self.evaluator = StageEvaluator(session)
        self.resources = ResourceManager()
        self.aborted = False
        self._interrupt_notice = False
        self._writer: Writer | None = None
        self.commits: Committer

    @property
    def writer(self) -> Writer:
        assert self._writer is not None
        return self._writer

    # -- entry ---------------------------------------------------------------

    def run(self) -> RunResult:
        session = self.session
        repo = session.repo
        assert repo is not None, "run needs a writable session"
        started = time.monotonic()
        with (
            pipeline_lock(session.root, exclusive=False),
            branch_lock(session.root, session.branch.name),
        ):
            check_storage_roots(repo.storages(), session.storage_roots)
            repo.set_meta(identity_meta(session))
            repo.upsert_storages(session.storage_roots)
            branch = session.branch
            repo.upsert_branch(
                BranchRow(
                    branch_id=session.branch_id,
                    name=branch.name,
                    config=branch.config_json,
                    active_axes=branch.active_axes_json,
                    is_temp=branch.is_temp,
                    manual=branch.manual,
                )
            )
            # The code's checkout, wherever the command was started (roots may be relocated).
            commit, dirty = git_state(session.pipeline.module_file().parent)
            repo.insert_run(
                RunRow(
                    run_id=self.run_id,
                    branch_id=session.branch_id,
                    started_at=now_iso(),
                    status="running",
                    cli_argv=list(self.options.argv if self.options.argv is not None else sys.argv),
                    host=socket.gethostname(),
                    git_commit=commit,
                    git_dirty=dirty,
                )
            )
            for note in recover(session, self.run_id):
                self.bus.publish(Notice(f"recovery: {note}", "warning"))
            self._writer = Writer(repo, session.fingerprints.cache)
            self.commits = Committer(session, self.run_id, self._writer, self.evaluator, self.bus)
            self.bus.publish(
                RunStarted(
                    session.pipeline.selector(), branch.name, self.run_id, tuple(self.selected)
                )
            )
            try:
                with self._sigint():
                    asyncio.run(self._drive())
            except KeyboardInterrupt:
                self.aborted = True
                self.stop.request("interrupted")
            except BaseException:
                wall_ms = int((time.monotonic() - started) * 1000)
                repo.finish_run(self.run_id, status="failed", total_wall_ms=wall_ms)
                raise
            finally:
                self.writer.flush()
                for root in session.storage_roots.values():
                    shutil.rmtree(layout.staging_root(root, self.run_id), ignore_errors=True)
            status = self._status()
            wall_ms = int((time.monotonic() - started) * 1000)
            repo.finish_run(self.run_id, status=status, total_wall_ms=wall_ms)
            if any(item.executed or item.linked for item in self.outcomes.values()):
                # Rows this run replaced may have left key parts, source details,
                # or file sets unreferenced.
                with suppress(sqlite3.OperationalError):
                    repo.collect_garbage()
        rotate_logs(session.root, active=repo.running_run_ids())
        self.bus.publish(RunFinished(status, wall_ms))
        return RunResult(self.run_id, session.branch.name, status, self.outcomes, wall_ms)

    def _status(self) -> RunStatus:
        if self.stop.reason == "interrupted" or self.aborted:
            return "interrupted"
        bad = ("failed", "blocked", "interrupted")
        return "failed" if any(item.status in bad for item in self.outcomes.values()) else "ok"

    @contextmanager
    def _sigint(self) -> Iterator[None]:
        """First Ctrl-C: stop submitting and let active units commit; second: abort."""

        if threading.current_thread() is not threading.main_thread():
            yield
            return
        count = 0

        def handle(_signum: int, _frame: Any) -> None:
            # Only flags here: the handler may run inside any code holding a lock
            # (event bus, logging, sqlite); the main loop publishes the notice.
            nonlocal count
            count += 1
            if count > 1:
                raise KeyboardInterrupt
            self.stop.request("interrupted")
            self._interrupt_notice = True

        previous = signal.signal(signal.SIGINT, handle)
        try:
            yield
        finally:
            signal.signal(signal.SIGINT, previous)

    def _tick(self) -> None:
        """Main-loop housekeeping between scheduler polls."""

        if self._interrupt_notice:
            self._interrupt_notice = False
            self.bus.publish(
                Notice("interrupt: finishing active units (Ctrl-C again to abort)", "warning")
            )
        self.writer.maybe_flush()

    # -- stages --------------------------------------------------------------

    async def _drive(self) -> None:
        graph = self.session.graph
        selected = set(self.selected)
        errors: dict[str, str] = {}
        for name in self.needed:
            self._tick()
            node = graph.nodes[name]
            is_selected = name in selected
            if is_selected and self.stop.requested:
                self._finish(StageOutcome(name, "skipped", detail="run interrupted"))
                continue
            problem = self._dependency_problem(node, selected, errors)
            if problem is not None:
                if is_selected:
                    self._finish(StageOutcome(name, problem[0], detail=problem[1]))
                else:
                    self.states[name] = StageState([])
                continue
            try:
                plan = self.evaluator.evaluate(
                    node,
                    self.states,
                    force=is_selected and self.options.force,
                    find_orphans=is_selected,
                )
            except (ProbeError, GraphError, RefResolutionError, OSError, ValueError) as error:
                if is_selected:
                    self._finish(StageOutcome(name, "failed", detail=str(error)))
                else:
                    errors[name] = str(error)
                    self.states[name] = StageState([])
                continue
            if not is_selected:
                self.states[name] = probe_state(self.session, plan)
                continue
            if plan.status == "awaiting":
                detail = f"awaiting {plan.awaiting!r}"
                self._finish(StageOutcome(name, "blocked", detail=detail))
                continue
            await self._execute(node, plan)
        self._tick()

    def _dependency_problem(
        self, node: StageNode, selected: set[str], errors: dict[str, str]
    ) -> tuple[StageStatus, str] | None:
        """Why ``node`` cannot run: a selected dependency did not complete (skip), or a
        dependency outside the selection is not current (blocked)."""

        for dep in node.deps:
            state = self.states[dep]
            if state.complete:
                continue
            if dep in selected:
                return "skipped", f"upstream {dep!r} did not complete"
            if dep in errors:
                return "blocked", f"upstream {dep!r} cannot be evaluated: {errors[dep]}"
            if state.awaiting is not None or state.units:
                return "blocked", f"upstream {dep!r} is not current; include it (e.g. --upto)"
            return "blocked", f"upstream {dep!r} is not available"
        return None

    def _finish(self, outcome: StageOutcome) -> None:
        self.outcomes[outcome.name] = outcome
        if outcome.name not in self.states:
            self.states[outcome.name] = StageState([])
        if outcome.status in ("blocked", "skipped", "failed") and outcome.total == 0:
            self.bus.publish(StagePlanned(outcome.name, outcome.status, detail=outcome.detail))
        self.bus.publish(
            StageFinished(
                outcome.name,
                outcome.status,
                outcome.executed,
                outcome.hit + outcome.linked,
                outcome.failed,
                outcome.wall_ms,
                outcome.detail,
            )
        )

    async def _execute(self, node: StageNode, plan: StagePlan) -> None:
        session = self.session
        repo = session.repo
        assert repo is not None
        started = time.monotonic()
        name = node.name
        storage_root = session.storage_roots[node.storage]
        outcome = StageOutcome(name, "ok", total=len(plan.units))
        if plan.orphans:
            outcome.orphaned = repo.mark_orphaned(session.branch_id, name, plan.orphans)
        state = StageState([unit.unit for unit in plan.units])
        self.states[name] = state
        for unit_plan in plan.units:
            row = unit_plan.row
            if unit_plan.decision == "hit" and row is not None:
                assert row.artifact_fp is not None and row.artifact_relpath is not None
                state.artifacts[unit_plan.unit_key] = (
                    row.artifact_fp,
                    session.storage_roots[row.storage_name] / row.artifact_relpath,
                )
        outcome.hit = len(state.artifacts)
        to_run = [unit for unit in plan.units if unit.decision != "hit"]
        if self.options.failed_only:
            to_run = [u for u in to_run if u.row is not None and u.row.status == "failed"]
        if not self.options.force and to_run:
            remaining = []
            for unit_plan in to_run:
                if self.commits.link(node, unit_plan, state):
                    outcome.linked += 1
                else:
                    remaining.append(unit_plan)
            to_run = remaining

        run_ctx = RunCtx(session.config, session.args, session.branch.name, name, session.root)
        try:
            workers = node.resolve_workers(run_ctx)
        except Exception as error:
            outcome.status, outcome.detail = "failed", str(error)
            self._finish(outcome)
            return
        backend_name = resolve_backend(node, workers)
        self.bus.publish(
            StagePlanned(
                name,
                "ready",
                total=len(plan.units),
                hit=outcome.hit,
                to_run=len(to_run),
                linked=outcome.linked,
                workers=workers,
                backend=backend_name,
            )
        )
        durations: list[int] = []
        metrics: dict[str, float] = {}
        rss: list[float] = []
        halted = None
        stage_run = StageRunRow(
            run_id=self.run_id,
            branch_id=session.branch_id,
            stage_name=name,
            status="running",
            executed_units=0,
            hit_units=outcome.hit + outcome.linked,
            failed_units=0,
            total_units=outcome.total,
            workers=workers,
            wall_ms=0,
            finished_at=now_iso(),
        )
        repo.put_stage_run(stage_run)
        if to_run:
            try:
                halted = await self._run_units(
                    node, to_run, state, workers, backend_name, outcome, durations, metrics, rss
                )
            except (ResourceError, OSError) as error:
                outcome.status, outcome.detail = "failed", str(error)
        self.writer.flush()
        complete = len(state.artifacts) == len(plan.units)
        state.complete = complete
        if complete and outcome.status == "ok":
            try:
                unit_dirs = {
                    unit.unit_key: session.unit_dir(name, node.storage, unit.relpath)
                    for unit in state.units
                }
                check_publish_collisions(
                    node,
                    state.units,
                    (
                        (session.graph.nodes[other], item.units)
                        for other, item in self.states.items()
                    ),
                    self.publish_claims,
                )
                publish_stage(node, state.units, unit_dirs, storage_root, session.publish_dir)
            except (PublishError, GraphError, OSError) as error:
                outcome.status, outcome.detail = "failed", f"publish failed: {error}"
                state.complete = False
        if outcome.status == "ok" and not complete:
            if halted == "broken-pool":
                outcome.status = "failed"
                outcome.detail = "a process-pool worker crashed or was killed (see stderr)"
            elif outcome.failed:
                outcome.status = "failed"
                outcome.detail = f"{outcome.failed} unit(s) failed"
                if halted == "max-failures":
                    outcome.detail += f" (stopped at max_failures={node.max_failures})"
            elif halted == "interrupted":
                outcome.status = "interrupted"
            else:
                outcome.status = "partial"
                outcome.detail = "some units were not selected (--failed-only)"
        outcome.wall_ms = int((time.monotonic() - started) * 1000)
        peak = max(rss) if rss else (peak_rss_mb() if outcome.executed else None)
        stage_run.status = outcome.status
        stage_run.executed_units = outcome.executed
        stage_run.hit_units = outcome.hit + outcome.linked
        stage_run.failed_units = outcome.failed
        stage_run.wall_ms = outcome.wall_ms
        stage_run.finished_at = now_iso()
        stage_run.unit_p50_ms = percentile(durations, 0.5)
        stage_run.unit_p95_ms = percentile(durations, 0.95)
        stage_run.unit_max_ms = max(durations) if durations else None
        stage_run.peak_rss_mb = peak
        stage_run.metrics_summary = metrics or None
        repo.put_stage_run(stage_run)
        self._finish(outcome)

    # -- execution -----------------------------------------------------------

    async def _run_units(
        self,
        node: StageNode,
        to_run: list[UnitPlan],
        state: StageState,
        workers: int,
        backend_name: str,
        outcome: StageOutcome,
        durations: list[int],
        metrics: dict[str, float],
        rss: list[float],
    ) -> str | None:
        session = self.session
        name = node.name
        capture = self.tui_active or workers > 1
        table = ResourceTable()
        unit_resources = {u.unit_key: table.indices(u.cell.resources) for u in to_run}
        run_ctx = RunCtx(session.config, session.args, session.branch.name, name, session.root)
        resource_access: dict[int, list[ConfigAccess]] = {}
        for index, access in self.resources.preflight(table.resources, run_ctx).items():
            resource_access.setdefault(index, []).append(access)
        keys: dict[str, InputKey] = {}

        def probe_key(unit_plan: UnitPlan) -> InputKey:
            cached = keys.get(unit_plan.unit_key)
            if cached is None:
                access = unit_plan.row.config_access if unit_plan.row is not None else None
                cached = self.commits.input_key(node, unit_plan, access)
                keys[unit_plan.unit_key] = cached
            return cached

        jobs: dict[str, UnitJob] = {}
        # Unit staging dirs are created by the units themselves, below this one.
        (layout.staging_root(session.storage_roots[node.storage], self.run_id) / name).mkdir(
            parents=True, exist_ok=True
        )

        def job_for(unit_plan: UnitPlan) -> UnitJob:
            job = jobs.get(unit_plan.unit_key)
            if job is None:
                job = self._job(node, unit_plan, capture, unit_resources[unit_plan.unit_key])
                jobs[unit_plan.unit_key] = job
            return job

        def probe_access(unit_plan: UnitPlan) -> list[str] | None:
            return unit_plan.row.config_access if unit_plan.row is not None else None

        def on_start(unit_plan: UnitPlan) -> None:
            key = probe_key(unit_plan)
            row = self.commits.row(node, unit_plan, key, probe_access(unit_plan), "running")
            # A unit with a previous row is marked before its body starts (a hard
            # kill then leaves it `running`); a new unit's mark is batched, since
            # without a row it probes as `new` anyway. Either mark reaches the
            # store before a promotion replaces an existing unit dir.
            self.writer.put(row, key.file_sets, sync=unit_plan.row is not None)
            self.bus.publish(UnitStarted(name, unit_plan.unit_key))

        broken: list[str] = []

        def on_done(unit_plan: UnitPlan, result: UnitOutcome | BaseException) -> bool:
            job = jobs[unit_plan.unit_key]
            if isinstance(result, BrokenProcessPool):
                broken.append(str(result))
            if isinstance(result, BaseException):
                result = UnitOutcome(
                    unit_plan.unit_key,
                    ok=False,
                    duration_ms=0,
                    error_type=type(result).__name__,
                    error_summary=str(result) or type(result).__name__,
                )
            if result.ok:
                try:
                    self.commits.commit_ok(node, unit_plan, job, result, state)
                except Exception as error:
                    result.ok = False
                    result.error_type = type(error).__name__
                    what = "promote" if isinstance(error, OSError) else "commit"
                    result.error_summary = f"{what} failed: {error}"
                    result.error_traceback = traceback.format_exc()
            if not result.ok:
                remove_tree(job.staging)
                # A failed execution's reads are incomplete: key the failure on the whole
                # Config, so a later Config fix shows as `config-changed`, not `failed`.
                key = self.commits.input_key(node, unit_plan, None)
                row = self.commits.row(node, unit_plan, key, None, "failed")
                row.error_type = result.error_type
                row.error_summary = result.error_summary
                row.error_traceback = result.error_traceback
                row.duration_ms = result.duration_ms
                row.metrics = result.metrics or None
                self.writer.put(row, key.file_sets)
                outcome.failed += 1
            outcome.executed += 1
            durations.append(result.duration_ms)
            if result.peak_rss_mb is not None:
                rss.append(result.peak_rss_mb)
            if result.ok:
                for key_name, value in result.metrics.items():
                    if isinstance(value, int | float) and not isinstance(value, bool):
                        metrics[key_name] = metrics.get(key_name, 0) + value
            self.bus.publish(
                UnitFinished(
                    name,
                    unit_plan.unit_key,
                    result.ok,
                    result.duration_ms,
                    None if result.ok else f"{result.error_type}: {result.error_summary}",
                )
            )
            self.writer.maybe_flush()
            return not result.ok

        async with AsyncExitStack() as stack:
            shared, opened_access = await self.resources.open_shared(
                table.resources, run_ctx, stack, backend=backend_name
            )
            for index, access in opened_access.items():
                resource_access.setdefault(index, []).append(access)
            cells = [(u.unit.cell, tuple(unit_resources[u.unit_key].values())) for u in to_run]
            if capture and backend_name != "process":
                # In-process units log through routing proxies on sys.stdout/stderr.
                stack.enter_context(routed_std_streams())
            backend, queue_cleanup = self._backend(
                node, backend_name, workers, shared, resource_access, cells, capture
            )
            try:
                result = await run_window(
                    to_run,
                    lambda unit_plan: backend.submit(job_for(unit_plan)),
                    workers=workers,
                    is_exclusive=lambda unit_plan: node.is_exclusive(unit_plan.unit),
                    max_failures=node.max_failures,
                    stop=self.stop,
                    on_start=on_start,
                    on_done=on_done,
                    on_started=self.writer.flush_running,
                    on_tick=self._tick,
                    should_halt=lambda: "broken-pool" if broken else None,
                )
            except BaseException:
                backend.close(abort=True)
                queue_cleanup()
                raise
            backend.close()
            queue_cleanup()
        return result.halted

    def _backend(
        self,
        node: StageNode,
        backend_name: str,
        workers: int,
        shared: dict[int, Any],
        resource_access: dict[int, list[ConfigAccess]],
        cells: list[tuple[Cell, tuple[int, ...]]],
        capture: bool,
    ) -> tuple[Backend, Any]:
        session = self.session
        if backend_name == "process":
            import multiprocessing

            queue = multiprocessing.get_context("spawn").Queue()
            thread = pump(queue, self.bus)
            setup = WorkerSetup(
                pipeline=PipelineLocator.of(session.pipeline),
                stage=node.name,
                config=session.config,
                args=session.args,
                branch=session.branch.name,
                root=session.root,
                shared=shared,
                shared_access={
                    index: [access.resolve() for access in items]
                    for index, items in resource_access.items()
                },
                cells=distinct_cells(cells),
                env=dict(session.workspace.worker_env),
                queue=queue,
            )

            def cleanup() -> None:
                queue.put(None)
                thread.join(timeout=2)
                queue.close()

            return ProcessBackend(setup, workers), cleanup
        sink: CtxSink = BusSink(self.bus, tqdm_fallback=not capture)
        state = WorkerState(
            instance=session.pipeline(),
            node=node,
            config=session.config,
            args=session.args,
            branch=session.branch.name,
            root=session.root,
            sessions=shared,
            sink=sink,
            resource_access=resource_access,
        )
        backend: Backend
        if backend_name == "thread":
            backend = ThreadBackend(state, workers)
        elif backend_name == "async":
            backend = AsyncBackend(state)
        else:
            backend = InlineBackend(state)
        return backend, lambda: None

    def _job(
        self, node: StageNode, unit_plan: UnitPlan, capture: bool, resources: dict[str, int]
    ) -> UnitJob:
        session = self.session
        unit = unit_plan.unit
        values = dict(zip((axis.name for axis in unit.axes), unit.values, strict=True))
        kwargs = {
            param.name: values[param.name]
            if param.kind == "axis"
            else unit_plan.injections[param.name]
            for param in node.params
            if param.kind in ("axis", "upstream", "ref")
        }
        storage_root = session.storage_roots[node.storage]
        staging = layout.staging_dir(storage_root, self.run_id, node.name, unit.unit_key)
        if staging.exists():
            remove_tree(staging)
        log_path = (
            layout.unit_log_path(session.root, self.run_id, node.name, unit.unit_key)
            if capture
            else None
        )
        return UnitJob(
            stage=node.name,
            unit_key=unit.unit_key,
            coords=unit.coords,
            cell=unit.cell,
            staging=staging,
            kwargs=kwargs,
            upstream=dict(unit_plan.injections),
            log_path=log_path,
            resources=resources,
        )
