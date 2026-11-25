"""One unit's lifecycle pieces.

Worker side (any backend): build ``Ctx``, open unit-scope resources, call
the stage, require ``None``, check ``produces``, fingerprint the staging dir
and make managed files read-only. Main side: promote the staging dir over
the final unit dir (``.old-*`` protected) before the store commit.
"""

from __future__ import annotations

import os
import resource as resource_usage
import shutil
import sys
import time
import traceback
from collections.abc import Mapping
from contextlib import AsyncExitStack, ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from varve.axis import Cell
from varve.context import Ctx, CtxSink
from varve.dependencies import DepCtx
from varve.engine.capture import fd_output, unit_output
from varve.engine.resources import recording_ctx
from varve.graph import StageNode
from varve.keying.config_access import ConfigAccess, merge_access, record_config
from varve.keying.fingerprint import ArtifactFingerprint, artifact_fingerprint, walk_files
from varve.pipeline import Pipeline
from varve.resource import Resource, RunCtx

READ_ONLY = 0o444
DIR_MODE = 0o755


@dataclass(frozen=True)
class UnitJob:
    """Everything a worker needs to run one unit (picklable for process pools)."""

    stage: str
    unit_key: str
    coords: Mapping[str, str]
    cell: Cell
    staging: Path
    kwargs: Mapping[str, Any]
    """Axis values and upstream/Ref injections by parameter name."""
    upstream: Mapping[str, Any]
    """Everything ``ctx.upstream(name)`` serves (injected and ``needs=``-only)."""
    log_path: Path | None
    resources: Mapping[str, int] = field(default_factory=dict)
    """Resource name -> index in the stage's ``ResourceTable`` (this unit's cell)."""


@dataclass
class UnitOutcome:
    unit_key: str
    ok: bool
    duration_ms: int
    metrics: dict[str, Any] = field(default_factory=dict)
    config_access: list[str] | None = None
    artifact: ArtifactFingerprint | None = None
    error_type: str | None = None
    error_summary: str | None = None
    error_traceback: str | None = None
    peak_rss_mb: float | None = None


@dataclass
class WorkerState:
    """Per-worker execution context (main process for in-process backends)."""

    instance: Pipeline
    node: StageNode
    config: Any
    args: Any
    branch: str
    root: Path
    sessions: dict[int, Any]
    """Opened stage/worker-scope resource sessions by ``ResourceTable`` index."""
    sink: CtxSink
    process_worker: bool = False
    resource_access: dict[int, list[ConfigAccess]] = field(default_factory=dict)
    """Config reads of each shared resource's preflight/open, by table index."""

    def run_ctx(self, cell: Cell | None = None) -> RunCtx:
        return RunCtx(
            self.config, self.args, self.branch, self.node.name, self.root, cell or Cell()
        )

    def unit_resources(self, cell: Cell) -> list[Resource[Any]]:
        resources = self.node.resources_for(DepCtx(self.config, cell))
        return [resource for resource in resources if resource.scope == "unit"]

    def unit_access(self, job: UnitJob, access: ConfigAccess) -> list[str] | None:
        """The unit's own Config reads plus those of the shared resources it uses."""

        resolved = access.resolve()
        for index in set(job.resources.values()):
            for item in self.resource_access.get(index, ()):
                resolved = merge_access(resolved, item.resolve())
        return resolved


class StageReturnError(TypeError):
    pass


def _ctx(state: WorkerState, job: UnitJob, access: ConfigAccess) -> Ctx[Any, Any]:
    return Ctx(
        out=job.staging,
        config=record_config(state.config, access),
        args=state.args,
        branch=state.branch,
        stage=job.stage,
        unit_key=job.unit_key,
        coords=job.coords,
        cell=job.cell,
        upstream=job.upstream,
        sink=state.sink,
    )


def _kwargs(state: WorkerState, job: UnitJob, ctx: Ctx[Any, Any], unit: Mapping[str, Any]):
    kwargs: dict[str, Any] = {}
    for param in state.node.params:
        if param.kind == "ctx":
            kwargs[param.name] = ctx
        elif param.kind == "resource":
            index = job.resources.get(param.name)
            if param.name in unit:
                kwargs[param.name] = unit[param.name]
            elif index is not None and index in state.sessions:
                kwargs[param.name] = state.sessions[index]
            else:
                raise RuntimeError(
                    f"Resource {param.name!r} has no open session for stage {job.stage!r}"
                )
        else:
            kwargs[param.name] = job.kwargs[param.name]
    return kwargs


def _check_return(stage: str, result: Any) -> None:
    if result is not None:
        raise StageReturnError(
            f"Stage {stage!r} returned {type(result).__name__}; stages must return None "
            "and write their outputs under ctx.out"
        )


def _failure(job: UnitJob, error: BaseException, started: float, ctx: Ctx[Any, Any] | None):
    shutil.rmtree(job.staging, ignore_errors=True)
    lines = traceback.format_exception(type(error), error, error.__traceback__)
    summary = str(error).strip().splitlines()
    return UnitOutcome(
        job.unit_key,
        ok=False,
        duration_ms=_elapsed_ms(started),
        metrics=dict(ctx.metrics) if ctx is not None else {},
        error_type=type(error).__name__,
        error_summary=(summary[0] if summary else type(error).__name__)[:500],
        error_traceback="".join(lines),
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


def peak_rss_mb() -> float:
    """Peak resident set size of this process in MiB (``ru_maxrss`` is KiB on Linux)."""

    usage = resource_usage.getrusage(resource_usage.RUSAGE_SELF).ru_maxrss
    return usage / (1024 * 1024) if sys.platform == "darwin" else usage / 1024


def execute(state: WorkerState, job: UnitJob) -> UnitOutcome:
    """Run one sync unit and finalize its staging dir (inline/thread/process)."""

    started = time.monotonic()
    access = ConfigAccess()
    ctx: Ctx[Any, Any] | None = None
    capture = fd_output if state.process_worker else unit_output
    try:
        job.staging.mkdir(parents=True, exist_ok=False)
        with capture(job.log_path), ExitStack() as stack:
            ctx = _ctx(state, job, access)
            sessions: dict[str, Any] = {}
            for resource in state.unit_resources(job.cell):
                if resource.open is None:
                    continue
                manager = resource.open(recording_ctx(state.run_ctx(job.cell), access))
                if not hasattr(manager, "__enter__"):
                    raise TypeError(
                        f"Resource {resource.name!r} (scope='unit') returned an async context "
                        f"manager; stage {job.stage!r} is not async"
                    )
                sessions[resource.name] = stack.enter_context(manager)  # type: ignore[arg-type]
            method = getattr(state.instance, job.stage)
            _check_return(job.stage, method(**_kwargs(state, job, ctx, sessions)))
        artifact = finalize_staging(job.staging, state.node)
    except BaseException as error:
        if isinstance(error, KeyboardInterrupt | SystemExit) and not state.process_worker:
            shutil.rmtree(job.staging, ignore_errors=True)
            raise
        return _failure(job, error, started, ctx)
    return UnitOutcome(
        job.unit_key,
        ok=True,
        duration_ms=_elapsed_ms(started),
        metrics=dict(ctx.metrics),
        config_access=state.unit_access(job, access),
        artifact=artifact,
        peak_rss_mb=peak_rss_mb() if state.process_worker else None,
    )


async def execute_async(state: WorkerState, job: UnitJob) -> UnitOutcome:
    """Run one ``async def`` unit; finalization runs in a thread."""

    import asyncio

    started = time.monotonic()
    access = ConfigAccess()
    ctx: Ctx[Any, Any] | None = None
    try:
        job.staging.mkdir(parents=True, exist_ok=False)
        with unit_output(job.log_path):
            async with AsyncExitStack() as stack:
                ctx = _ctx(state, job, access)
                sessions: dict[str, Any] = {}
                for resource in state.unit_resources(job.cell):
                    if resource.open is None:
                        continue
                    manager = resource.open(recording_ctx(state.run_ctx(job.cell), access))
                    if hasattr(manager, "__aenter__"):
                        session = await stack.enter_async_context(manager)  # type: ignore[arg-type]
                    else:
                        session = stack.enter_context(manager)  # type: ignore[arg-type]
                    sessions[resource.name] = session
                method = getattr(state.instance, job.stage)
                _check_return(job.stage, await method(**_kwargs(state, job, ctx, sessions)))
        artifact = await asyncio.to_thread(finalize_staging, job.staging, state.node)
    except asyncio.CancelledError:
        shutil.rmtree(job.staging, ignore_errors=True)
        raise
    except Exception as error:
        return _failure(job, error, started, ctx)
    return UnitOutcome(
        job.unit_key,
        ok=True,
        duration_ms=_elapsed_ms(started),
        metrics=dict(ctx.metrics),
        config_access=state.unit_access(job, access),
        artifact=artifact,
    )


def finalize_staging(staging: Path, node: StageNode) -> ArtifactFingerprint:
    """Check ``produces``, fingerprint the managed files, and make them read-only."""

    spec = node.spec
    for relpath in spec.produces or ():
        if not (staging / relpath).exists():
            raise FileNotFoundError(
                f"Stage {node.name!r} declares produces={relpath!r} but ctx.out / "
                f"{relpath!r} was not written"
            )
    if spec.produces_file is not None and not (staging / spec.produces_file).is_file():
        raise FileNotFoundError(
            f"Stage {node.name!r}: produces={spec.produces_file!r} must be a regular file"
        )
    artifact = artifact_fingerprint(staging)
    make_read_only(staging)
    return artifact


def make_read_only(root: Path) -> None:
    """Managed files become 0444; directories stay 0755 (so units can be replaced/cleaned)."""

    for directory, dirs, files in os.walk(root):
        os.chmod(directory, DIR_MODE)
        for name in files:
            os.chmod(os.path.join(directory, name), READ_ONLY)
        for name in dirs:
            os.chmod(os.path.join(directory, name), DIR_MODE)


def old_dir(final: Path, run_id: str) -> Path:
    return final.with_name(f"{final.name}.old-{run_id}")


def promote(staging: Path, final: Path, run_id: str) -> None:
    """Atomically replace ``final`` with ``staging`` (the previous dir is kept as ``.old-*``)."""

    final.parent.mkdir(parents=True, exist_ok=True)
    previous: Path | None = None
    if final.exists() or final.is_symlink():
        previous = old_dir(final, run_id)
        os.rename(final, previous)
    try:
        os.rename(staging, final)
    except OSError:
        if previous is not None:
            try:  # put the previous unit dir back before the unit is marked failed
                os.rename(previous, final)
            except OSError:
                pass  # startup recovery restores `.old-*` through the `running` row
        raise
    if previous is not None:
        remove_tree(previous)


def remove_tree(path: Path) -> None:
    """Remove a managed tree (read-only files live in writable directories)."""

    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def link_tree(source: Path, staging: Path) -> None:
    """Hardlink every managed file of ``source`` into a fresh ``staging`` dir."""

    staging.mkdir(parents=True, exist_ok=False)
    for relpath, path in walk_files(source):
        target = staging / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        os.link(path, target)
    for directory, dirs, _ in os.walk(staging):
        os.chmod(directory, DIR_MODE)
        for name in dirs:
            os.chmod(os.path.join(directory, name), DIR_MODE)
