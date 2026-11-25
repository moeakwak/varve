"""Stage expansion and per-unit decisions, shared by ``status``/``plan``/``run``.

Stages are evaluated in topological order. Each evaluation needs the
:class:`StageState` of its dependencies: which upstream units are available
(``hit`` during a read-only probe, ``ok`` after execution during a run) with
their artifact fingerprints and paths. A dynamic axis whose resolver reads a
stage that is not complete leaves the stage ``awaiting``; a unit whose
upstream units are not all available cannot be keyed (``upstream-pending``).
"""

from __future__ import annotations

import json
import os
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from stat import S_ISREG
from typing import Any, Literal

from varve.axis import AxisDomain, Cell
from varve.dependencies import DepCtx, Dependencies
from varve.engine.refs import RefBinding
from varve.engine.session import Session
from varve.graph import (
    DynamicRequest,
    StageNode,
    Unit,
    Upstream,
    align_request,
    align_units,
    dynamic_requests,
    expand_stage,
)
from varve.keying.config_access import project_config
from varve.keying.fingerprint import InputDigest, json_hash, normalize_json, verify_manifest
from varve.keying.keys import (
    SEGMENT_REASONS,
    changed_segments,
    combine_hashes,
    inputs_hash,
    plain_hash,
    source_match,
    version_hash,
)
from varve.keying.source import SourceFingerprint
from varve.resource import Resource
from varve.store import layout
from varve.store.repo import ProbeRow

Decision = Literal["hit", "needs-run", "failed"]
StageStatus = Literal["ready", "awaiting"]


_KEY_CHANGED = "<key-changed>"


class ProbeError(RuntimeError):
    """A stage cannot be evaluated (resolver, dependency, or binding failure)."""


@dataclass
class StageState:
    """What downstream evaluations may read about one evaluated stage."""

    units: list[Unit]
    artifacts: dict[str, tuple[str, Path]] = field(default_factory=dict)
    """unit_key -> (artifact_fp, injection path) of available units."""
    complete: bool = False
    awaiting: str | None = None
    """Root upstream a still-unexpanded stage waits for."""


@dataclass
class CellEval:
    """Per-cell (static coordinates) resources, dependencies and fingerprints."""

    resources: tuple[Resource[Any], ...]
    deps: Dependencies
    inputs: dict[str, InputDigest]
    inputs_hash: str
    values: dict[str, Any]
    values_hash: str
    source: SourceFingerprint
    plain_values: dict[str, Any]


@dataclass(slots=True)
class UnitPlan:
    unit: Unit
    decision: Decision
    reason: str | None
    labels: tuple[str, ...]
    row: ProbeRow | None
    cell: CellEval
    injections: dict[str, Any]
    """Upstream/Ref name -> injected value (``Path`` or ``dict``); missing when pending."""
    upstream_keys: dict[str, Any] | None
    """Upstream/Ref name -> key entry; ``None`` when an upstream unit is unavailable."""
    input_key: str | None
    hashes: dict[str, str] | None
    axis_inputs: dict[str, InputDigest]
    reasons: tuple[str, ...] = ()

    @property
    def unit_key(self) -> str:
        return self.unit.unit_key

    def inputs(self) -> dict[str, InputDigest]:
        return self.cell.inputs | self.axis_inputs if self.axis_inputs else self.cell.inputs

    def values(self) -> dict[str, Any]:
        return self.cell.values | self.unit.axis_values()


@dataclass
class StagePlan:
    node: StageNode
    status: StageStatus
    awaiting: str | None = None
    units: list[UnitPlan] = field(default_factory=list)
    orphans: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.node.name

    def counts(self) -> dict[str, int]:
        counts = {"hit": 0, "needs-run": 0, "failed": 0}
        for plan in self.units:
            counts[plan.decision] += 1
        return counts


def _upstream_entry(binding: Upstream, state: StageState) -> tuple[Any, Any] | None:
    """(key entry, injected value) for one aligned binding, or None if unavailable."""

    artifacts = state.artifacts
    if isinstance(binding, Mapping):
        entry: list[list[str]] = []
        value: dict[Any, Path] = {}
        for key, unit in binding.items():
            found = artifacts.get(unit.unit_key)
            if found is None:
                return None
            entry.append([unit.unit_key, found[0]])
            value[key] = found[1]
        return entry, value
    found = artifacts.get(binding.unit_key)
    if found is None:
        return None
    return found[0], found[1]


def _plain_values(values: Mapping[str, Any]) -> dict[str, Any]:
    return {
        name: value
        if isinstance(value, str | int | float | bool) or value is None
        else normalize_json(value)
        for name, value in values.items()
    }


class StageEvaluator:
    """Evaluates stages of one session; caches cell evaluations and hashes."""

    def __init__(self, session: Session) -> None:
        self.session = session
        self._config_hashes: dict[str | None, str] = {}
        self._sources: dict[tuple[Any, ...], SourceFingerprint] = {}
        self._version_hashes: dict[str, str] = {}
        self._stage_dirs: dict[str, str] = {}

    # -- hashing -------------------------------------------------------------

    def config_hash(self, access_json: str | None) -> str:
        """Hash of the Config projected on a stored ``config_access_json`` (memoized)."""

        cached = self._config_hashes.get(access_json)
        if cached is None:
            access = None if access_json is None else json.loads(access_json)
            cached = json_hash(project_config(self.session.config_dump, access))
            self._config_hashes[access_json] = cached
        return cached

    def version_hash(self, node: StageNode) -> str:
        """``version`` segment hash: ``version=`` plus the normalized ``produces=``."""

        cached = self._version_hashes.get(node.name)
        if cached is None:
            cached = version_hash(node.spec.version, node.spec.produces_key)
            self._version_hashes[node.name] = cached
        return cached

    # -- cells ---------------------------------------------------------------

    def cell_eval(self, node: StageNode, cell: Cell) -> CellEval:
        try:
            return self._cell_eval(node, cell)
        except (TypeError, ValueError) as error:
            where = f" for cell {dict(cell)}" if cell else ""
            raise ProbeError(
                f"Stage {node.name!r}: cannot evaluate resources/dependencies{where}: {error}"
            ) from error

    def _cell_eval(self, node: StageNode, cell: Cell) -> CellEval:
        session = self.session
        depctx = DepCtx(session.config, cell)
        resources = node.resources_for(depctx)
        names = {resource.name for resource in resources}
        missing = [p.name for p in node.params if p.kind == "resource" and p.name not in names]
        if missing:
            raise ProbeError(
                f"Stage {node.name!r}: parameters {missing} are not resources of cell "
                f"{dict(cell)} (resources: {sorted(names)})"
            )
        deps = node.dependencies_for(depctx, resources)
        dynamic = {axis.name for axis in node.dynamic_axes}
        clash = sorted((set(deps.inputs) | set(deps.values)) & dynamic)
        if clash:
            raise ProbeError(
                f"Stage {node.name!r}: Dependencies names {clash} clash with axis names"
            )
        inputs = session.fingerprints.inputs(deps.inputs)
        values = dict(deps.values)
        source_key = (node.name, tuple(str(item) for item in deps.sources))
        source = self._sources.get(source_key)
        if source is None:
            source = session.sources.stage_source(session.pipeline, node.name, deps.sources)
            self._sources[source_key] = source
        plain = normalize_json(values)
        return CellEval(
            resources, deps, inputs, inputs_hash(inputs), values, plain_hash(plain), source, plain
        )

    # -- resolvers -----------------------------------------------------------

    def resolve(self, request: DynamicRequest, states: Mapping[str, StageState]) -> AxisDomain:
        """Full (unrestricted) domain of one resolver request, memoized by upstream fps."""

        session = self.session
        binding = session.graph.resolvers[request.axis]
        kwargs: dict[str, Any] = {}
        fps: list[Any] = []
        for param in binding.params:
            if param.kind == "ctx":
                kwargs[param.name] = DepCtx(session.config, request.cell)
            elif param.kind == "axis":
                kwargs[param.name] = request.cell[param.name]
            elif param.kind == "upstream":
                up_node = session.graph.nodes[param.name]
                state = states[param.name]
                aligned = align_request(request, up_node, state.units)
                found = _upstream_entry(aligned, state)
                if found is None:
                    raise ProbeError(f"Upstream {param.name!r} is not available")
                fps.append(found[0])
                kwargs[param.name] = found[1]
            elif param.kind == "ref":
                ref = session.graph.refs[param.name]
                bound = session.refs.bind(param.name, ref, request.static_axes, [request.ids])
                fps.append(bound[0].key)
                kwargs[param.name] = bound[0].value
        memo = (request.axis, request.ids, plain_hash(fps))
        domain = session.resolved.get(memo)
        if domain is None:
            assert request.axis.resolver is not None
            try:
                result = request.axis.resolver(**kwargs)
                domain = request.axis.domain_from_resolved(result)
            except Exception as error:
                where = f" for {dict(request.cell)}" if request.ids else ""
                raise ProbeError(
                    f"Axis {request.axis.name!r} resolver failed{where}: "
                    f"{type(error).__name__}: {error}"
                ) from error
            session.resolved[memo] = domain
        session.full_domains[request.key] = domain
        return domain

    def restrict(self, request: DynamicRequest, domain: AxisDomain) -> AxisDomain:
        branch = self.session.branch
        name = request.axis.name
        ids = branch.active.get(name)
        if ids is not None:
            unknown = [item for item in ids if item not in set(domain.ids)]
            if unknown:
                raise ProbeError(
                    f"--slice {name}: ids {unknown} are not produced by the axis resolver"
                    + (f" for {dict(request.cell)}" if request.ids else "")
                )
            domain = domain.restrict(ids)
        sample = branch.samples.get(name)
        if sample is not None:
            scope = ",".join([name, *request.ids])
            domain = domain.restrict(sample.pick(domain.ids, scope))
        return domain

    # -- stages --------------------------------------------------------------

    def evaluate(
        self,
        node: StageNode,
        states: Mapping[str, StageState],
        *,
        force: bool = False,
        find_orphans: bool = False,
    ) -> StagePlan:
        session = self.session
        graph = session.graph
        waiting = self._awaiting(node, states)
        if waiting is not None:
            return StagePlan(node, "awaiting", awaiting=waiting)

        dynamics: dict[Any, AxisDomain] = {}
        for request in dynamic_requests(graph, node, session.statics):
            if request.key in dynamics:
                continue
            dynamics[request.key] = self.restrict(request, self.resolve(request, states))
        units = expand_stage(graph, node, session.statics, dynamics)
        rows = session.repo.probe_rows(session.branch_id, node.name) if session.repo else {}

        bindings: dict[str, list[Upstream]] = {
            name: align_units(node, units, graph.nodes[name], states[name].units)
            for name in node.upstream
        }
        ref_bindings: dict[str, Sequence[RefBinding]] = {}
        coords = [unit.ids for unit in units] if node.refs else []
        for name in node.refs:
            bound = session.refs.bind(name, graph.refs[name], node.axes, coords)
            if len(bound) != len(units):
                raise ProbeError(f"Ref resolver returned {len(bound)} bindings for {name!r}")
            ref_bindings[name] = bound

        static_cells = not node.cell_dependent
        cells: dict[tuple[str, ...], CellEval] = {}
        static_positions = [i for i, axis in enumerate(node.axes) if axis.is_static]
        version_hash = self.version_hash(node)
        old_components: dict[str, dict[str, Any]] | None = None
        plans: list[UnitPlan] = []
        for index, unit in enumerate(units):
            cell_key = () if static_cells else tuple(unit.ids[i] for i in static_positions)
            cell = cells.get(cell_key)
            if cell is None:
                cell = self.cell_eval(node, unit.cell)
                cells[cell_key] = cell

            injections: dict[str, Any] = {}
            upstream_keys: dict[str, Any] | None = {}
            for name, aligned in bindings.items():
                found = _upstream_entry(aligned[index], states[name])
                if found is None:
                    upstream_keys = None
                    continue
                if upstream_keys is not None:
                    upstream_keys[name] = found[0]
                injections[name] = found[1]
            for name, bound in ref_bindings.items():
                if upstream_keys is not None:
                    upstream_keys[name] = bound[index].key
                injections[name] = bound[index].value

            row = rows.get(unit.unit_key)
            axis_inputs = unit.axis_inputs()
            axis_digests = session.fingerprints.inputs(axis_inputs) if axis_inputs else {}
            input_key = hashes = None
            if upstream_keys is not None:
                axis_values = unit.axis_values()
                try:
                    values_hash = (
                        plain_hash(cell.plain_values | _plain_values(axis_values))
                        if axis_values
                        else cell.values_hash
                    )
                except (TypeError, ValueError) as error:
                    raise ProbeError(
                        f"Stage {node.name!r}: dynamic axis values of unit "
                        f"{unit.unit_key!r} cannot be keyed: {error}"
                    ) from error
                hashes = {
                    "config": self.config_hash(row.config_access_json if row else None),
                    "upstream": plain_hash(upstream_keys),
                    "inputs": inputs_hash(cell.inputs | axis_digests)
                    if axis_digests
                    else cell.inputs_hash,
                    "values": values_hash,
                    "version": version_hash,
                }
                input_key = combine_hashes(hashes)

            decision, reason, labels, reasons = self._decide(
                node, unit, row, cell, input_key, force=force
            )
            if reason == _KEY_CHANGED:
                assert row is not None and hashes is not None
                if old_components is None:
                    old_components = (
                        session.repo.key_components(session.branch_id, node.name)
                        if session.repo is not None
                        else {}
                    )
                old = old_components.get(unit.unit_key, {})
                segments = changed_segments(
                    old, {seg: {"hash": value} for seg, value in hashes.items()}
                )
                reasons = tuple(SEGMENT_REASONS[seg] for seg in segments) or ("input-changed",)
                match = source_match(
                    version=node.spec.version,
                    current=cell.source.fingerprint,
                    recorded=row.source_fp,
                    reused=row.reused_source_fp,
                )
                if match == "changed":
                    reasons = (*reasons, "source-changed")
                reason = reasons[0]
            plan = UnitPlan(
                unit,
                decision,
                reason,
                labels,
                row,
                cell,
                injections,
                upstream_keys,
                input_key,
                hashes,
                axis_digests,
                reasons,
            )
            plans.append(plan)

        stage_plan = StagePlan(node, "ready", units=plans)
        if find_orphans:
            stage_plan.orphans = self._orphans(node, units, rows)
        return stage_plan

    def _awaiting(self, node: StageNode, states: Mapping[str, StageState]) -> str | None:
        for dep in node.deps:
            state = states.get(dep)
            if state is None:
                raise ProbeError(f"Stage {node.name!r} evaluated before its dependency {dep!r}")
            if state.awaiting is not None:
                return state.awaiting
        resolver_stages = {
            stage
            for axis in node.dynamic_axes
            for stage in self.session.graph.resolvers[axis].stages
        }
        for dep in node.deps:
            if dep in resolver_stages and not states[dep].complete:
                return dep
        return None

    def _decide(
        self,
        node: StageNode,
        unit: Unit,
        row: ProbeRow | None,
        cell: CellEval,
        input_key: str | None,
        *,
        force: bool,
    ) -> tuple[Decision, str | None, tuple[str, ...], tuple[str, ...]]:
        if force:
            return "needs-run", "forced", (), ("forced",)
        if row is None or row.status == "orphaned":
            return "needs-run", "new", (), ("new",)
        if row.status == "running":
            return "needs-run", "interrupted", (), ("interrupted",)
        if input_key is None:
            return "needs-run", "upstream-pending", (), ("upstream-pending",)
        if input_key != row.input_key:
            return "needs-run", _KEY_CHANGED, (), ()
        match = source_match(
            version=node.spec.version,
            current=cell.source.fingerprint,
            recorded=row.source_fp,
            reused=row.reused_source_fp,
        )
        if match == "changed":
            return "needs-run", "source-changed", (), ("source-changed",)
        labels: list[str] = []
        if match in ("reused", "code-modified"):
            labels.append(match)
        if row.status == "failed":
            return "failed", row.error_summary, tuple(labels), ()
        if not self._artifact_ok(node, unit, row):
            reason = "artifact-changed" if self.session.rehash else "artifact-missing"
            return "needs-run", reason, (), (reason,)
        if row.linked_from:
            labels.append(f"linked@{row.linked_from}")
        return "hit", None, tuple(labels), ()

    def _artifact_ok(self, node: StageNode, unit: Unit, row: ProbeRow) -> bool:
        root = self.session.storage_roots.get(row.storage_name)
        if root is None or row.artifact_fp is None or row.storage_name != node.storage:
            return False
        relpath = unit.relpath
        base = self._stage_dirs.get(node.name)
        if base is None:
            base = str(layout.internal_dir(root, self.session.branch.name, node.name, ""))
            self._stage_dirs[node.name] = base
        unit_dir = f"{base}/{relpath}" if relpath else base
        manifest = row.artifact_manifest
        if self.session.rehash:
            return verify_manifest(Path(unit_dir), manifest, rehash=True)
        for rel, size, mtime_ns, _digest in manifest:
            try:
                st = os.lstat(f"{unit_dir}/{rel}")
            except FileNotFoundError:
                return False
            if not S_ISREG(st.st_mode) or st.st_size != size or st.st_mtime_ns != mtime_ns:
                return False
        return True

    def _orphans(
        self, node: StageNode, units: Sequence[Unit], rows: Mapping[str, ProbeRow]
    ) -> list[str]:
        """Rows of this stage no longer produced by a (fully resolved) dynamic axis."""

        if not node.dynamic_axes:
            return []
        session = self.session
        active = {unit.unit_key for unit in units}
        names = [axis.name for axis in node.axes]
        positions = {axis.name: i for i, axis in enumerate(node.axes)}
        static_ids = {axis: set(session.statics[axis].ids) for axis in node.static_axes}
        domain_ids: dict[int, set[str]] = {}
        orphans: list[str] = []
        for key, row in rows.items():
            if key in active or row.status == "orphaned":
                continue
            pairs = [part.partition("=") for part in key.split(",")] if key else []
            if [name for name, _, _ in pairs] != names:
                continue
            ids = [item for _, _, item in pairs]
            if any(ids[positions[axis.name]] not in static_ids[axis] for axis in static_ids):
                continue
            for axis in node.dynamic_axes:
                bound = session.graph.resolvers[axis].static_axes
                domain = session.full_domains.get(
                    (axis, tuple(ids[positions[item.name]] for item in bound))
                )
                if domain is None:
                    continue
                known = domain_ids.setdefault(id(domain), set(domain.ids))
                if ids[positions[axis.name]] not in known:
                    orphans.append(key)
                    break
        return orphans


def probe_state(session: Session, plan: StagePlan) -> StageState:
    """Downstream view of a probed stage: ``hit`` units are available."""

    if plan.status == "awaiting":
        return StageState([], awaiting=plan.awaiting)
    artifacts: dict[str, tuple[str, Path]] = {}
    for unit_plan in plan.units:
        row = unit_plan.row
        if unit_plan.decision == "hit" and row is not None:
            assert row.artifact_fp is not None and row.artifact_relpath is not None
            root = session.storage_roots[row.storage_name]
            artifacts[unit_plan.unit_key] = (row.artifact_fp, root / row.artifact_relpath)
    units = [unit_plan.unit for unit_plan in plan.units]
    return StageState(units, artifacts, complete=len(artifacts) == len(units))


def probe(session: Session, stages: Collection[str] | None = None) -> dict[str, StagePlan]:
    """Read-only evaluation of ``stages`` (default: all) and their ancestors, in order."""

    graph = session.graph
    needed = set(graph.order)
    if stages is not None:
        needed = set(stages)
        for name in stages:
            needed.update(graph.ancestors(name))
    evaluator = StageEvaluator(session)
    states: dict[str, StageState] = {}
    plans: dict[str, StagePlan] = {}
    for name in graph.order:
        if name not in needed:
            continue
        plan = evaluator.evaluate(graph.nodes[name], states)
        plans[name] = plan
        states[name] = probe_state(session, plan)
    return plans
