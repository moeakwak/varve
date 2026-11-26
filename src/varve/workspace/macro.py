"""Macro orchestration: ``(pipeline, branch)`` nodes linked by cross-pipeline Refs.

Nodes are the registered pipelines (or the given targets) times their
``varve.yaml`` branches (``manual`` branches only with ``--all`` or when
targeted explicitly). Every Ref of a node pulls the upstream node of each
referenced branch in, recursively. Nodes run sequentially, in-process, in
topological order (ties broken by ``varve.toml`` order, then name, then
branch declaration order); the first failure stops the run.
"""

from __future__ import annotations

import heapq
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter, ValidationError

from varve.branch import load_branches
from varve.cli.argmap import is_null_token
from varve.engine.run import RunOptions, RunResult
from varve.engine.session import main_config
from varve.pipeline import Pipeline
from varve.ref import Ref
from varve.workspace.config import WorkspaceSettings
from varve.workspace.discovery import (
    UNREGISTERED,
    Entry,
    TargetError,
    discover,
    match_entries,
    resolve_entry,
)
from varve.workspace.modules import ensure_on_path, load_pipeline, locate_module


class MacroError(ValueError):
    """The macro plan cannot be built (unknown target, cycle, bad ``--set``)."""


class Workspace:
    """Discovered entries plus on-demand pipeline imports."""

    def __init__(self, settings: WorkspaceSettings) -> None:
        self.settings = settings
        ensure_on_path(settings.workspace_root)
        self.entries: list[Entry] = discover(settings)
        self._classes: dict[str, type[Pipeline]] = {}

    def resolve(self, query: str) -> Entry:
        """A pipeline by selector rules; an importable module not yet known is accepted too."""

        try:
            return resolve_entry(self.entries, query)
        except TargetError:
            if match_entries(self.entries, query):
                raise
            location = locate_module(query)
            if location is None:
                raise
        entry = Entry(name=query, module=query, location=location)
        pipeline = self.pipeline(entry)
        return self.entry_for_class(pipeline)

    def pipeline(self, entry: Entry) -> type[Pipeline]:
        cached = self._classes.get(entry.name)
        if cached is not None:
            return cached
        target = entry.import_target
        if target is None:
            raise TargetError(f"Store of {entry.name!r} at {entry.db_path} records no module")
        try:
            pipeline = load_pipeline(*target)
        except ImportError as error:
            raise TargetError(f"Cannot import {target[0]!r} for {entry.name!r}: {error}") from error
        self._classes[entry.name] = pipeline
        return pipeline

    def entry_for_class(self, pipeline: type[Pipeline]) -> Entry:
        name = pipeline.selector()
        for entry in self.entries:
            if entry.name == name:
                self._classes.setdefault(name, pipeline)
                return entry
        entry = Entry(
            name=name,
            module=pipeline.import_module_name(),
            qualname=pipeline.__qualname__,
            order=UNREGISTERED,
        )
        self.entries.append(entry)
        self._classes[name] = pipeline
        return entry

    def root_for(self, entry: Entry, pipeline: type[Pipeline]) -> Path | None:
        """The discovered store's root when it differs from the default root, else ``None``."""

        if self.settings.root_base is not None or entry.db_path is None or entry.root is None:
            return None
        default = Path(pipeline.default_root(main_config(pipeline))).absolute()
        try:
            same = default.resolve() == entry.root.resolve()
        except OSError:
            same = default == entry.root
        return None if same else entry.root

    def upstream_entry(self, ref: Ref, owner: str) -> Entry:
        if isinstance(ref.target, str):
            try:
                return self.resolve(ref.target)
            except TargetError as error:
                raise MacroError(
                    f"{owner}: Ref target {ref.target!r} is not a workspace pipeline ({error})"
                ) from error
        return self.entry_for_class(ref.target)


def branches_of(pipeline: type[Pipeline]) -> dict[str, bool]:
    """Declared branches (``main`` first) -> ``manual``; temporary branches excluded."""

    declared = load_branches(pipeline.varve_config_path())
    branches = {"main": declared["main"].manual if "main" in declared else False}
    for name, item in declared.items():
        if not item.is_temporary:
            branches.setdefault(name, item.manual)
    return branches


@dataclass(frozen=True)
class Node:
    entry: Entry
    pipeline: type[Pipeline]
    branch: str
    branch_index: int

    @property
    def label(self) -> str:
        return f"{self.entry.name}@{self.branch}"

    def sort_key(self) -> tuple[int, str, int]:
        return (self.entry.order, self.entry.name, self.branch_index)


@dataclass
class MacroPlan:
    nodes: list[Node]
    """All nodes in execution (topological) order."""
    upstream: dict[str, list[str]]
    """label -> labels of the nodes its Refs read."""
    selected: list[str]
    """Labels that will run (after ``--only`` / ``--skip`` / ``--from``)."""
    why: dict[str, str]
    """label -> why it is in the plan (``target`` or ``required by ...``)."""
    sets: dict[str, dict[str, Any]] = field(default_factory=dict)
    """label -> coerced ``--set`` Args values routed to it."""

    def node(self, label: str) -> Node:
        return next(node for node in self.nodes if node.label == label)


def split_target(text: str) -> tuple[str, str | None]:
    module, sep, branch = text.partition("@")
    if not module or (sep and not branch):
        raise MacroError(f"Invalid target {text!r}; expected MODULE or MODULE@BRANCH")
    return module, branch or None


def build_plan(
    workspace: Workspace,
    targets: Sequence[str] = (),
    *,
    all_branches: bool = False,
    branch: str | None = None,
    only: Sequence[str] = (),
    skip: Sequence[str] = (),
    start: str | None = None,
    sets: Mapping[str, str] | None = None,
) -> MacroPlan:
    nodes: dict[str, Node] = {}
    why: dict[str, str] = {}

    def add(entry: Entry, pipeline: type[Pipeline], name: str, reason: str) -> Node:
        branches = branches_of(pipeline)
        label = f"{entry.name}@{name}"
        if label not in nodes:
            index = list(branches).index(name) if name in branches else len(branches)
            nodes[label] = Node(entry, pipeline, name, index)
            why[label] = reason
        return nodes[label]

    def add_pipeline(entry: Entry, explicit: str | None, *, required: bool) -> None:
        pipeline = workspace.pipeline(entry)
        branches = branches_of(pipeline)
        if explicit is not None:
            if explicit not in branches:
                raise MacroError(
                    f"{entry.name} has no branch {explicit!r} (branches: {', '.join(branches)})"
                )
            add(entry, pipeline, explicit, "target")
            return
        if branch is not None:
            if branch in branches:
                add(entry, pipeline, branch, "target")
            elif required:
                raise MacroError(
                    f"{entry.name} has no branch {branch!r} (branches: {', '.join(branches)})"
                )
            return
        for name, manual in branches.items():
            if all_branches or not manual:
                add(entry, pipeline, name, "target")

    if targets:
        for text in targets:
            module, explicit = split_target(text)
            add_pipeline(_resolve(workspace, module), explicit, required=True)
    else:
        registered = [entry for entry in workspace.entries if entry.entry is not None]
        if not registered:
            raise MacroError(
                "No pipelines registered: add [workspace] pipelines to varve.toml or pass targets"
            )
        for entry in registered:
            add_pipeline(entry, None, required=False)
    if not nodes:
        raise MacroError("No (pipeline, branch) matches the given targets and --branch")

    upstream: dict[str, list[str]] = {}
    queue = list(nodes.values())
    while queue:
        node = queue.pop(0)
        deps: list[str] = []
        for attr, ref in node.pipeline.graph().refs.items():
            owner = f"{node.label}.{attr}"
            entry = workspace.upstream_entry(ref, owner)
            pipeline = workspace.pipeline(entry)
            for name in ref.branches:
                label = f"{entry.name}@{name}"
                fresh = label not in nodes
                up = add(entry, pipeline, name, f"required by {node.label}")
                if fresh:
                    queue.append(up)
                if up.label not in deps:
                    deps.append(up.label)
        upstream[node.label] = deps

    order = _topological(nodes, upstream)
    labels = [node.label for node in order]
    selected = list(labels)
    if only:
        wanted = {label for text in only for label in _matching(workspace, text, order)}
        selected = [label for label in selected if label in wanted]
    if skip:
        dropped = {label for text in skip for label in _matching(workspace, text, order)}
        selected = [label for label in selected if label not in dropped]
    if start is not None:
        first = _matching(workspace, start, order)
        cut = min(labels.index(label) for label in first)
        selected = [label for label in selected if labels.index(label) >= cut]
    plan = MacroPlan(order, upstream, selected, why)
    plan.sets = route_sets(plan, sets or {})
    return plan


def _resolve(workspace: Workspace, module: str) -> Entry:
    try:
        return workspace.resolve(module)
    except TargetError as error:
        raise MacroError(str(error)) from error


def _matching(workspace: Workspace, text: str, order: Sequence[Node]) -> list[str]:
    module, explicit = split_target(text)
    entry = _resolve(workspace, module)
    found = [
        node.label
        for node in order
        if node.entry.name == entry.name and (explicit is None or node.branch == explicit)
    ]
    if not found:
        raise MacroError(f"{text!r} matches no node of this plan")
    return found


def _topological(nodes: Mapping[str, Node], upstream: Mapping[str, list[str]]) -> list[Node]:
    remaining = {label: set(upstream.get(label, ())) for label in nodes}
    dependents: dict[str, list[str]] = {label: [] for label in nodes}
    for label, deps in remaining.items():
        for dep in deps:
            dependents[dep].append(label)
    heap = [(nodes[label].sort_key(), label) for label, deps in remaining.items() if not deps]
    heapq.heapify(heap)
    order: list[Node] = []
    while heap:
        _, label = heapq.heappop(heap)
        order.append(nodes[label])
        for child in dependents[label]:
            remaining[child].discard(label)
            if not remaining[child]:
                heapq.heappush(heap, (nodes[child].sort_key(), child))
    if len(order) != len(nodes):
        cycle = sorted(label for label, deps in remaining.items() if deps)
        raise MacroError(f"Ref cycle between {', '.join(cycle)}")
    return order


def route_sets(plan: MacroPlan, sets: Mapping[str, str]) -> dict[str, dict[str, Any]]:
    """Route ``--set field=value`` to the selected nodes whose Args declare ``field``.

    Values are coerced by the field's type (as given, then as JSON); a key no
    selected pipeline declares is an error.
    """

    routed: dict[str, dict[str, Any]] = {}
    for key, raw in sets.items():
        matched = False
        for label in plan.selected:
            args_model = plan.node(label).pipeline.Args
            info = args_model.model_fields.get(key)
            if info is None:
                continue
            matched = True
            routed.setdefault(label, {})[key] = _coerce(info.annotation, raw, key, label)
        if not matched:
            names = sorted({plan.node(label).entry.name for label in plan.selected})
            raise MacroError(
                f"--set {key}: no selected pipeline declares an Args field {key!r} "
                f"({', '.join(names)})"
            )
    return routed


def _coerce(annotation: Any, raw: str, key: str, label: str) -> Any:
    if is_null_token(annotation, raw):  # like the generated CLI: `null` is None
        return None
    adapter: TypeAdapter[Any] = TypeAdapter(annotation if annotation is not None else Any)
    try:
        return adapter.validate_python(raw)
    except ValidationError as first:
        try:
            return adapter.validate_json(raw)
        except ValidationError:
            raise MacroError(f"--set {key}={raw!r} is invalid for {label}: {first}") from first


def parse_sets(items: Sequence[str]) -> dict[str, str]:
    sets: dict[str, str] = {}
    for item in items:
        key, sep, value = item.partition("=")
        key = key.strip().replace("-", "_")
        if not sep or not key:
            raise MacroError(f"Invalid --set {item!r}; expected FIELD=VALUE")
        sets[key] = value
    return sets


# -- execution -----------------------------------------------------------------


@dataclass(frozen=True)
class NodeOutcome:
    label: str
    status: str
    """``ok``, ``failed``, ``interrupted``, ``error`` (raised before/while running), ``skipped``,
    or ``not-run`` (after an earlier failure)."""
    wall_ms: int = 0
    result: RunResult | None = None
    error: str | None = None

    @property
    def executed(self) -> int:
        return sum(stage.executed for stage in self.result.stages.values()) if self.result else 0

    @property
    def hit(self) -> int:
        if self.result is None:
            return 0
        return sum(stage.hit + stage.linked for stage in self.result.stages.values())


@dataclass
class MacroResult:
    outcomes: list[NodeOutcome]
    failed: str | None = None
    """Label of the node that stopped the run."""
    exit_code: int = 0


def run_plan(
    workspace: Workspace,
    plan: MacroPlan,
    *,
    force: bool = False,
    rehash: bool = False,
    tui: bool | None = None,
    argv: Sequence[str] | None = None,
    before_node: Callable[[Node], None] | None = None,
    errors: tuple[type[BaseException], ...] = (ValueError, RuntimeError, OSError),
) -> MacroResult:
    """Run the selected nodes in order; stop at the first one that does not end ``ok``."""

    from varve.engine.service import run_pipeline

    result = MacroResult([])
    stopped = False
    for node in plan.nodes:
        label = node.label
        if stopped:
            result.outcomes.append(NodeOutcome(label, "not-run"))
            continue
        if label not in plan.selected:
            result.outcomes.append(NodeOutcome(label, "skipped"))
            continue
        if before_node is not None:
            before_node(node)
        started = time.perf_counter()
        try:
            run = run_pipeline(
                node.pipeline,
                branch=node.branch,
                args=plan.sets.get(label) or None,
                options=RunOptions(force=force, rehash=rehash, tui=tui, argv=list(argv or ())),
                root=workspace.root_for(node.entry, node.pipeline),
                workspace=workspace.settings,
            )
        except errors as error:
            wall = int((time.perf_counter() - started) * 1000)
            result.outcomes.append(NodeOutcome(label, "error", wall, error=str(error)))
            result.failed, result.exit_code, stopped = label, 1, True
            continue
        wall = int((time.perf_counter() - started) * 1000)
        result.outcomes.append(NodeOutcome(label, run.status, wall, run))
        if run.status != "ok":
            result.failed, result.exit_code, stopped = label, run.exit_code, True
    return result
