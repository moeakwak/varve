"""In-process single-pipeline services used by the generated CLI and the workspace layer.

Every function takes the Pipeline class plus keyword options and never
parses argv, so the top-level ``varve`` command can call them directly.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from varve.branch_config import BranchRequest
from varve.engine.capture import terminal_to_process_log
from varve.engine.clean import CleanPlan, clean_pipeline, plan_clean
from varve.engine.events import Event, EventBus
from varve.engine.probe import ProbeError, StagePlan, probe
from varve.engine.refs import RefInfo, RefResolver, bound_refs
from varve.engine.run import Runner, RunOptions, RunResult
from varve.engine.session import Session, WorkspaceSettings, identity_meta, open_session
from varve.engine.tui import LiveDisplay, PlainReporter
from varve.pipeline import Pipeline
from varve.status import InspectView, PipelineView, inspect_unit, pipeline_view
from varve.store import layout
from varve.store.lock import branch_lock, pipeline_lock

ArgsInput = BaseModel | Mapping[str, Any] | None


def _request(
    branch: str,
    overrides: Mapping[str, Any] | str | None = None,
    temp: bool = False,
    slices: Mapping[str, Sequence[str]] | None = None,
    samples: Mapping[str, int] | None = None,
    seed: int = 0,
) -> BranchRequest:
    return BranchRequest(branch, overrides, temp, dict(slices or {}), dict(samples or {}), seed)


def run_pipeline(
    pipeline: type[Pipeline],
    *,
    branch: str = "main",
    overrides: Mapping[str, Any] | str | None = None,
    args: ArgsInput = None,
    options: RunOptions | None = None,
    root: Path | None = None,
    temp: bool = False,
    slices: Mapping[str, Sequence[str]] | None = None,
    samples: Mapping[str, int] | None = None,
    seed: int = 0,
    refs: RefResolver | None = None,
    workspace: WorkspaceSettings | None = None,
    on_event: Callable[[Event], None] | None = None,
) -> RunResult:
    """Run one pipeline branch in-process.

    ``overrides`` (a mapping or JSON string) is merged over ``main``'s Config
    into a temporary branch; ``slices``/``samples`` narrow axes (persistent
    branches need ``temp=True`` or a new ``branch`` name). The TUI is used
    when ``options.tui`` is true (default: stdout is a terminal); otherwise
    events go to the ``varve`` logger. ``on_event`` receives every event.
    """

    options = options or RunOptions()
    request = _request(branch, overrides, temp, slices, samples, seed)
    session = open_session(
        pipeline,
        request,
        root=root,
        args=args,
        readonly=False,
        refs=refs,
        workspace=workspace,
        rehash=options.rehash,
    )
    try:
        bus = EventBus()
        if on_event is not None:
            bus.subscribe(on_event)
        runner = Runner(session, options, bus)
        tui = options.tui if options.tui is not None else sys.stdout.isatty()
        with ExitStack() as stack:
            if tui:
                process_log = layout.logs_dir(session.root, runner.run_id) / "process.log"
                terminal = stack.enter_context(terminal_to_process_log(process_log))
                display = stack.enter_context(LiveDisplay(terminal))
                bus.subscribe(display)
                runner.tui_active = True
            else:
                bus.subscribe(PlainReporter())
            return runner.run()
    finally:
        session.close()


def _probe_session(
    pipeline: type[Pipeline],
    *,
    branch: str,
    args: ArgsInput,
    root: Path | None,
    refs: RefResolver | None,
    workspace: WorkspaceSettings | None,
    rehash: bool = False,
    readonly: bool = True,
) -> Session:
    return open_session(
        pipeline,
        _request(branch),
        root=root,
        args=args,
        readonly=readonly,
        refs=refs,
        workspace=workspace,
        rehash=rehash,
    )


def probe_plans(session: Session, stages: Sequence[str] | None = None) -> dict[str, StagePlan]:
    for name in stages or ():
        if name not in session.graph.nodes:
            raise ValueError(f"Unknown stage {name!r}; stages: {list(session.graph.order)}")
    return probe(session, stages)


def probe_pipeline(
    pipeline: type[Pipeline],
    *,
    branch: str = "main",
    stages: Sequence[str] | None = None,
    args: ArgsInput = None,
    root: Path | None = None,
    refs: RefResolver | None = None,
    workspace: WorkspaceSettings | None = None,
    rehash: bool = False,
    expand: bool = False,
) -> PipelineView:
    """Read-only decisions for ``stages`` (default all; ancestors are evaluated too)."""

    session = _probe_session(
        pipeline, branch=branch, args=args, root=root, refs=refs, workspace=workspace, rehash=rehash
    )
    try:
        plans = probe_plans(session, stages)
        if stages is not None:
            plans = {name: plan for name, plan in plans.items() if name in stages}
        return pipeline_view(session, plans, expand=expand)
    finally:
        session.close()


def parse_unit_selector(text: str) -> tuple[str, dict[str, str]]:
    """``stage`` or ``stage@axis=id[,axis=id]`` (omitted axes are wildcards)."""

    stage, _, coords = text.partition("@")
    pairs: dict[str, str] = {}
    for part in filter(None, coords.split(",")):
        name, sep, item = part.partition("=")
        if not sep or not name or not item:
            raise ValueError(f"Invalid unit selector {text!r}; expected STAGE[@AXIS=ID,...]")
        pairs[name] = item
    return stage, pairs


@dataclass(frozen=True)
class InspectResult:
    stage: str
    units: list[tuple[str, str, str | None]]
    """(unit_key, decision, reason) of every matching unit."""
    view: InspectView | None
    """Details when exactly one unit matches."""
    source: dict[str, Any] | None
    """Current source fingerprint detail of the stage (first matching cell)."""
    refs: tuple[RefInfo, ...] = ()
    """Upstream records the stage's Refs read (committed state only)."""


def inspect_pipeline(
    pipeline: type[Pipeline],
    selector: str,
    *,
    branch: str = "main",
    args: ArgsInput = None,
    root: Path | None = None,
    refs: RefResolver | None = None,
    workspace: WorkspaceSettings | None = None,
) -> InspectResult:
    stage, coords = parse_unit_selector(selector)
    session = _probe_session(
        pipeline, branch=branch, args=args, root=root, refs=refs, workspace=workspace
    )
    try:
        plan = probe_plans(session, [stage])[stage]
        stage_refs = {*plan.node.refs, *plan.node.resolver_refs}
        ref_infos = tuple(info for info in bound_refs(session.refs) if info.attr in stage_refs)
        if plan.status == "awaiting":
            return InspectResult(stage, [], None, None, ref_infos)
        axes = {axis.name for axis in plan.node.axes}
        unknown = sorted(set(coords) - axes)
        if unknown:
            raise ValueError(f"Stage {stage!r} has no axes {unknown}; axes: {sorted(axes)}")
        matches = [
            unit
            for unit in plan.units
            if all(unit.unit.coords.get(name) == item for name, item in coords.items())
        ]
        listing = [
            (unit.unit_key, unit.decision, ", ".join(unit.reasons) if unit.reasons else unit.reason)
            for unit in matches
        ]
        view = inspect_unit(session, plan, matches[0]) if len(matches) == 1 else None
        source = matches[0].cell.source.detail if matches else None
        return InspectResult(stage, listing, view, source, ref_infos)
    finally:
        session.close()


@dataclass(frozen=True)
class ReuseResult:
    stage: str
    reused: int
    already: int
    not_reusable: dict[str, int]


def reuse_stage(
    pipeline: type[Pipeline],
    stage: str,
    *,
    branch: str = "main",
    args: ArgsInput = None,
    root: Path | None = None,
    refs: RefResolver | None = None,
    workspace: WorkspaceSettings | None = None,
) -> ReuseResult:
    """Exempt the current source change of ``stage``: ``ok`` units whose input key
    still matches get ``reused_source_fp = current source_fp``."""

    session = _probe_session(
        pipeline,
        branch=branch,
        args=args,
        root=root,
        refs=refs,
        workspace=workspace,
        readonly=False,
    )
    try:
        repo = session.repo
        assert repo is not None
        with pipeline_lock(session.root, exclusive=False), branch_lock(session.root, branch):
            repo.set_meta(identity_meta(session))
            plan = probe_plans(session, [stage])[stage]
            if plan.status == "awaiting":
                raise ProbeError(f"Stage {stage!r} is awaiting {plan.awaiting!r}; nothing to reuse")
            groups: dict[str, list[tuple[str, str]]] = {}
            already = 0
            not_reusable: dict[str, int] = {}
            for unit in plan.units:
                row = unit.row
                if unit.decision == "hit":
                    already += 1
                elif (
                    row is not None
                    and row.status == "ok"
                    and unit.input_key is not None
                    and unit.input_key == row.input_key
                ):
                    groups.setdefault(unit.cell.source.fingerprint, []).append(
                        (unit.unit_key, unit.input_key)
                    )
                else:
                    reason = unit.reason or unit.decision
                    not_reusable[reason] = not_reusable.get(reason, 0) + 1
            reused = 0
            with repo.transaction():
                for source_fp, matches in groups.items():
                    reused += repo.set_reused_source_fp(
                        session.branch_id, stage, matches, source_fp
                    )
            return ReuseResult(stage, reused, already, not_reusable)
    finally:
        session.close()


__all__ = [
    "CleanPlan",
    "InspectResult",
    "ReuseResult",
    "RunOptions",
    "RunResult",
    "clean_pipeline",
    "inspect_pipeline",
    "plan_clean",
    "probe_pipeline",
    "reuse_stage",
    "run_pipeline",
]
