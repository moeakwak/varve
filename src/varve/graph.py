"""Logical stage graph (binding, edges, validation) and Unit expansion/alignment.

``build_graph`` runs once per Pipeline class at ``Pipeline.graph()`` time and
checks everything that does not need a Config: parameter binding, edges from
parameters / ``needs=`` / resolvers, cycles, publish templates, exclusive
selectors. Expansion and alignment are pure functions over resolved axis
domains and use hash indexes, so they stay O(K) in the number of units.
"""

from __future__ import annotations

import heapq
import inspect
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from varve.axis import Axis, AxisDomain, Cell, FileMatch
from varve.decorators import (
    Backend,
    PublishEntry,
    ResourcesSpec,
    StageSpec,
    WorkersSpec,
    is_glob,
    is_positive_int,
)
from varve.dependencies import (
    DepCtx,
    Dependencies,
    DependsSpec,
    PathSet,
    merge_dependencies,
    resolve_dependencies,
)
from varve.ref import Ref
from varve.resource import Resource, RunCtx
from varve.signature import BindingError, Namespace, Param, bind_params
from varve.store import layout

if TYPE_CHECKING:
    from varve.pipeline import Pipeline

DEFAULT_KEYS = frozenset({"storage", "resources", "depends", "workers", "backend", "max_failures"})


class GraphError(ValueError):
    """An invalid pipeline declaration."""


class AlignmentError(GraphError):
    """A downstream coordinate has no matching upstream unit."""


@dataclass(frozen=True)
class ResolverBinding:
    """How a dynamic axis' resolver binds its parameters.

    For an ``Axis.files`` axis, ``params`` are the upstream stage's (static)
    axes followed by the upstream itself.
    """

    axis: Axis
    params: tuple[Param, ...]
    static_axes: tuple[Axis, ...]
    stages: tuple[str, ...]
    refs: tuple[str, ...]


@dataclass(frozen=True)
class StageNode:
    """One stage with its bindings and effective (defaults-merged) settings.

    ``upstream`` and ``refs`` are the stages / Refs whose artifacts enter the
    input key (injected parameters first, then ``needs=``). ``deps`` adds the
    stages that only dynamic-axis resolvers read, and ``resolver_refs`` the
    Refs they read: those order expansion but do not enter the key.
    """

    name: str
    spec: StageSpec
    axes: tuple[Axis, ...]
    params: tuple[Param, ...]
    upstream: tuple[str, ...]
    refs: tuple[str, ...]
    deps: tuple[str, ...]
    resolver_refs: tuple[str, ...]
    storage: str
    workers: WorkersSpec
    backend: Backend
    max_failures: int | None
    resources: tuple[ResourcesSpec, ...] = field(repr=False)
    depends: tuple[DependsSpec, ...] = field(repr=False)

    @property
    def static_axes(self) -> tuple[Axis, ...]:
        return tuple(axis for axis in self.axes if axis.is_static)

    @property
    def dynamic_axes(self) -> tuple[Axis, ...]:
        return tuple(axis for axis in self.axes if not axis.is_static)

    @property
    def cell_dependent(self) -> bool:
        """Whether resources or dependencies may differ between cells (any callable spec)."""

        for spec in self.resources:
            if callable(spec) or any(callable(resource.depends) for resource in spec):
                return True
        return any(callable(spec) for spec in self.depends)

    def resources_for(self, ctx: DepCtx) -> tuple[Resource[Any], ...]:
        """Resources of one cell: defaults then stage; a stage resource replaces a default
        resource of the same name, but one declaration may not name two resources alike."""

        merged: dict[str, Resource[Any]] = {}
        for spec in self.resources:
            items = spec(ctx) if callable(spec) else spec
            local: dict[str, Resource[Any]] = {}
            for resource in items:
                if not isinstance(resource, Resource):
                    raise TypeError(
                        f"Stage {self.name!r}: resources must be Resource objects, got {resource!r}"
                    )
                previous = local.setdefault(resource.name, resource)
                if previous is not resource:
                    raise ValueError(
                        f"Stage {self.name!r}: two different resources are named "
                        f"{resource.name!r} in one cell"
                    )
                merged[resource.name] = resource
        return tuple(merged.values())

    def dependencies_for(
        self, ctx: DepCtx, resources: Sequence[Resource[Any]] | None = None
    ) -> Dependencies:
        """Merged defaults + stage + resource dependencies of one cell."""

        if resources is None:
            resources = self.resources_for(ctx)
        labelled = [
            (f"stage {self.name!r}", resolve_dependencies(spec, ctx)) for spec in self.depends
        ]
        labelled += [
            (f"resource {resource.name!r}", resolve_dependencies(resource.depends, ctx))
            for resource in resources
        ]
        return merge_dependencies(*labelled)

    def resolve_workers(self, ctx: RunCtx) -> int:
        workers = self.workers(ctx) if callable(self.workers) else self.workers
        if not is_positive_int(workers):
            raise ValueError(
                f"Stage {self.name!r}: workers must be a positive int, got {workers!r}"
            )
        return workers

    def is_exclusive(self, unit: Unit) -> bool:
        """Whether ``unit`` matches any ``exclusive=`` selector (OR of AND-ed pairs)."""

        if not self.spec.exclusive:
            return False
        coords = dict(zip((axis.name for axis in unit.axes), unit.ids, strict=True))
        return any(
            all(coords.get(name) == item for name, item in rule) for rule in self.spec.exclusive
        )


@dataclass(frozen=True)
class StageGraph:
    """The validated logical graph of one Pipeline class, in topological order."""

    pipeline: type[Pipeline]
    nodes: Mapping[str, StageNode]
    axes: Mapping[str, Axis]
    refs: Mapping[str, Ref]
    resolvers: Mapping[Axis, ResolverBinding]

    @property
    def order(self) -> tuple[str, ...]:
        return tuple(self.nodes)

    def ancestors(self, name: str) -> list[str]:
        """Transitive dependencies of ``name`` (excluding it), in topological order."""

        seen: set[str] = set()
        pending = list(self.nodes[name].deps)
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(self.nodes[current].deps)
        return [item for item in self.nodes if item in seen]

    def descendants(self, name: str) -> list[str]:
        """Transitive dependents of ``name`` (excluding it), in topological order."""

        seen = {name}
        for current, node in self.nodes.items():
            if current != name and any(dep in seen for dep in node.deps):
                seen.add(current)
        return [item for item in self.nodes if item in seen and item != name]

    def storage_names(self) -> set[str]:
        return {node.storage for node in self.nodes.values()}

    def validate_storages(self, available: Collection[str]) -> None:
        unknown = sorted(
            f"{node.name} -> {node.storage!r}"
            for node in self.nodes.values()
            if node.storage not in available
        )
        if unknown:
            raise GraphError(
                f"Unknown storage name(s): {', '.join(unknown)}; declared storages: "
                f"{sorted(available)}"
            )


# ---------------------------------------------------------------------------
# Graph construction


def build_graph(pipeline: type[Pipeline]) -> StageGraph:
    specs = pipeline.stages()
    if not specs:
        raise GraphError(f"{pipeline.__qualname__} declares no stages")
    defaults = _check_defaults(pipeline)
    refs = dict(pipeline.refs())
    axes = _collect_axes(specs.values())
    for spec in specs.values():
        for need in spec.needs:
            if isinstance(need, Ref) and _ref_name(refs, need) is None:
                refs[f"ref:{need.describe()}"] = need
    _check_disjoint_names(specs, axes, refs)

    resolvers = {
        axis: _bind_resolver(axis, axes, specs, refs)
        for axis in axes.values()
        if not axis.is_static
    }
    nodes = {
        name: _build_node(spec, defaults, axes, specs, refs, resolvers)
        for name, spec in specs.items()
    }
    ordered = _topological(nodes)
    _check_publish_literals(nodes)
    return StageGraph(
        pipeline=pipeline,
        nodes={name: nodes[name] for name in ordered},
        axes=axes,
        refs=refs,
        resolvers=resolvers,
    )


def _check_defaults(pipeline: type[Pipeline]) -> Mapping[str, Any]:
    defaults = dict(pipeline.defaults)
    unknown = set(defaults) - DEFAULT_KEYS
    if unknown:
        raise GraphError(
            f"{pipeline.__qualname__}.defaults has unsupported keys {sorted(unknown)}; "
            f"allowed: {sorted(DEFAULT_KEYS)}"
        )
    workers, max_failures = defaults.get("workers"), defaults.get("max_failures")
    if workers is not None and not callable(workers) and not is_positive_int(workers):
        raise GraphError(f"{pipeline.__qualname__}.defaults['workers'] must be a positive int")
    if max_failures is not None and not is_positive_int(max_failures):
        raise GraphError(f"{pipeline.__qualname__}.defaults['max_failures'] must be a positive int")
    return defaults


def _collect_axes(specs: Iterable[StageSpec]) -> dict[str, Axis]:
    axes: dict[str, Axis] = {}
    for spec in specs:
        for axis in spec.foreach:
            existing = axes.setdefault(axis.name, axis)
            if existing is not axis:
                raise GraphError(
                    f"Two different Axis objects are named {axis.name!r}; "
                    "axis names must be unique within a pipeline"
                )
    return axes


def _ref_name(refs: Mapping[str, Ref], ref: Ref) -> str | None:
    return next((name for name, item in refs.items() if item is ref), None)


def _check_disjoint_names(
    specs: Mapping[str, StageSpec], axes: Mapping[str, Axis], refs: Mapping[str, Ref]
) -> None:
    groups = {"stage": set(specs), "axis": set(axes), "Ref": set(refs)}
    kinds = list(groups)
    for i, left in enumerate(kinds):
        for right in kinds[i + 1 :]:
            clash = groups[left] & groups[right]
            if clash:
                raise GraphError(f"Names used by both a {left} and a {right}: {sorted(clash)}")


def _bind_resolver(
    axis: Axis,
    axes: Mapping[str, Axis],
    specs: Mapping[str, StageSpec],
    refs: Mapping[str, Ref],
) -> ResolverBinding:
    assert axis.resolver is not None
    if isinstance(axis.resolver, FileMatch):
        return _bind_files(axis, axis.resolver, specs)
    static_names = [name for name, item in axes.items() if item.is_static]
    try:
        params = bind_params(
            axis.resolver,
            Namespace(axes=static_names, stages=specs, refs=refs, all_axes=axes),
            owner=f"Axis {axis.name!r} resolver",
            skip_self=False,
        )
    except BindingError as error:
        raise GraphError(str(error)) from None
    return ResolverBinding(
        axis=axis,
        params=params,
        static_axes=tuple(axes[p.name] for p in params if p.kind == "axis"),
        stages=tuple(p.name for p in params if p.kind == "upstream"),
        refs=tuple(p.name for p in params if p.kind == "ref"),
    )


def _bind_files(axis: Axis, files: FileMatch, specs: Mapping[str, StageSpec]) -> ResolverBinding:
    owner = f"Axis {axis.name!r} (Axis.files)"
    upstream = specs.get(files.upstream)
    if upstream is None:
        raise GraphError(
            f"{owner}: upstream {files.upstream!r} is not a stage of this pipeline "
            f"(stages: {', '.join(specs)})"
        )
    dynamic = [item.name for item in upstream.foreach if not item.is_static]
    if dynamic:
        raise GraphError(
            f"{owner}: upstream stage {files.upstream!r} has dynamic axes {dynamic}; "
            "Axis.files reads one unit directory per static cell"
        )
    params = tuple(Param(item.name, "axis") for item in upstream.foreach)
    return ResolverBinding(
        axis=axis,
        params=(*params, Param(files.upstream, "upstream")),
        static_axes=upstream.foreach,
        stages=(files.upstream,),
        refs=(),
    )


def _build_node(
    spec: StageSpec,
    defaults: Mapping[str, Any],
    axes: Mapping[str, Axis],
    specs: Mapping[str, StageSpec],
    refs: Mapping[str, Ref],
    resolvers: Mapping[Axis, ResolverBinding],
) -> StageNode:
    owner = f"Stage {spec.name!r}"
    resources = tuple(
        item for item in (defaults.get("resources"), spec.resources) if item is not None
    )
    depends = tuple(item for item in (defaults.get("depends"), spec.depends) if item is not None)
    static_resources: set[str] | None = set()
    for item in resources:
        if callable(item):
            static_resources = None
            break
        static_resources.update(resource.name for resource in item)

    shadowed = sorted((static_resources or set()) & ({*specs, *axes, *refs}))
    if shadowed:
        raise GraphError(f"{owner}: resource names clash with stage/axis/Ref names: {shadowed}")
    if spec.name in inspect.signature(spec.func).parameters:
        raise GraphError(f"{owner} cannot depend on itself")
    other_stages = [name for name in specs if name != spec.name]
    try:
        params = bind_params(
            spec.func,
            Namespace(
                axes=[axis.name for axis in spec.foreach],
                stages=other_stages,
                refs=refs,
                resources=static_resources,
                all_axes=axes,
            ),
            owner=owner,
            skip_self=True,
        )
    except BindingError as error:
        raise GraphError(str(error)) from None

    upstream = [p.name for p in params if p.kind == "upstream"]
    used_refs = [p.name for p in params if p.kind == "ref"]
    for need in spec.needs:
        if isinstance(need, Ref):
            name = _ref_name(refs, need)
            assert name is not None
            used_refs.append(name)
        elif need == spec.name:
            raise GraphError(f"{owner} cannot depend on itself")
        elif need in specs:
            upstream.append(need)
        elif need in refs:
            used_refs.append(need)
        else:
            raise GraphError(f"{owner} needs unknown stage {need!r}; known: {sorted(specs)}")

    resolver_stages: list[str] = []
    resolver_refs: list[str] = []
    for position, axis in enumerate(spec.foreach):
        binding = resolvers.get(axis)
        if binding is None:
            continue
        preceding = spec.foreach[:position]
        missing = [item.name for item in binding.static_axes if item not in preceding]
        if missing:
            raise GraphError(
                f"{owner}: resolver of axis {axis.name!r} reads static axes {missing} "
                "that must precede it in foreach="
            )
        if spec.name in binding.stages:
            raise GraphError(f"{owner}: resolver of axis {axis.name!r} reads the stage itself")
        resolver_stages.extend(binding.stages)
        resolver_refs.extend(binding.refs)

    _check_publish(spec)
    _check_exclusive(spec)
    workers = spec.workers if spec.workers is not None else defaults.get("workers", 1)
    backend = spec.backend if spec.backend is not None else defaults.get("backend", "auto")
    if backend not in ("auto", "async", "process", "thread"):
        raise GraphError(f"{owner}: unsupported backend {backend!r}")
    if spec.is_async and backend in ("process", "thread"):
        raise GraphError(f"{owner}: an async def stage cannot use backend={backend!r}")
    if not spec.is_async and backend == "async":
        raise GraphError(f"{owner}: backend='async' requires an async def stage")

    key_upstream = tuple(dict.fromkeys(upstream))
    return StageNode(
        name=spec.name,
        spec=spec,
        axes=spec.foreach,
        params=params,
        upstream=key_upstream,
        refs=tuple(dict.fromkeys(used_refs)),
        deps=tuple(dict.fromkeys([*key_upstream, *resolver_stages])),
        resolver_refs=tuple(dict.fromkeys(resolver_refs)),
        storage=spec.storage or defaults.get("storage") or layout.DEFAULT_STORAGE,
        workers=workers,
        backend=backend,
        max_failures=(
            spec.max_failures if spec.max_failures is not None else defaults.get("max_failures")
        ),
        resources=resources,
        depends=depends,
    )


def _check_publish(spec: StageSpec) -> None:
    owner = f"Stage {spec.name!r}"
    names = {axis.name for axis in spec.foreach}
    seen: set[str] = set()
    for entry in spec.publish:
        if entry.templated:
            used = entry.placeholders()
            unknown = used - names
            if unknown:
                raise GraphError(
                    f"{owner}: publish target {entry.dst!r} uses unknown axes {sorted(unknown)}"
                )
            if names and used != names and not entry.into_dir:
                raise GraphError(
                    f"{owner}: publish target {entry.dst!r} must use every axis placeholder "
                    f"{sorted('{' + name + '}' for name in names)} so units cannot collide"
                )
        if entry.dst in seen:
            raise GraphError(f"{owner}: two publish entries target {entry.dst!r}")
        seen.add(entry.dst)


def _check_publish_literals(nodes: Mapping[str, StageNode]) -> None:
    """Reject literal publish targets claimed by two stages (templated ones: at expansion)."""

    owners: dict[str, str] = {}
    for node in nodes.values():
        for entry in node.spec.publish:
            if entry.into_dir:
                continue  # a shared directory; its files are checked when published
            if entry.templated:
                literal = not entry.placeholders()
            else:
                literal = not node.axes and not is_glob(entry.src)
            if not literal:
                continue
            previous = owners.setdefault(entry.dst, node.name)
            if previous != node.name:
                raise GraphError(
                    f"Publish target {entry.dst!r} is claimed by both stage {previous!r} and "
                    f"stage {node.name!r}"
                )


def _check_exclusive(spec: StageSpec) -> None:
    by_name = {axis.name: axis for axis in spec.foreach}
    for rule in spec.exclusive:
        for name, item in rule:
            axis = by_name.get(name)
            if axis is None:
                raise GraphError(
                    f"Stage {spec.name!r}: exclusive selector axis {name!r} is not in foreach="
                )
            if axis.is_static and item not in axis.ids:
                raise GraphError(
                    f"Stage {spec.name!r}: exclusive selector {name}={item} is not a value of "
                    f"axis {name!r}"
                )


def _topological(nodes: Mapping[str, StageNode]) -> list[str]:
    """Kahn's algorithm preferring declaration order; reports a cycle path."""

    position = {name: index for index, name in enumerate(nodes)}
    indegree = {name: len(node.deps) for name, node in nodes.items()}
    dependents: dict[str, list[str]] = defaultdict(list)
    for name, node in nodes.items():
        for dep in node.deps:
            dependents[dep].append(name)
    ready = [(position[name], name) for name, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    ordered: list[str] = []
    while ready:
        _, name = heapq.heappop(ready)
        ordered.append(name)
        for dependent in dependents[name]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                heapq.heappush(ready, (position[dependent], dependent))
    if len(ordered) != len(nodes):
        cycle = " -> ".join(_find_cycle(nodes, ordered))
        raise GraphError(f"Stage dependency cycle: {cycle} (-> = depends on)")
    return ordered


def _find_cycle(nodes: Mapping[str, StageNode], done: Sequence[str]) -> list[str]:
    remaining = [name for name in nodes if name not in set(done)]
    path: list[str] = []
    current = remaining[0]
    while current not in path:
        path.append(current)
        current = next(dep for dep in nodes[current].deps if dep in remaining)
    return [*path[path.index(current) :], current]


# ---------------------------------------------------------------------------
# Expansion


@dataclass(frozen=True, slots=True)
class Unit:
    """One expanded execution/cache unit of a stage."""

    stage: str
    axes: tuple[Axis, ...]
    ids: tuple[str, ...]
    keys: tuple[Any, ...]
    values: tuple[Any, ...]
    unit_key: str

    @property
    def coords(self) -> dict[str, str]:
        return {axis.name: item for axis, item in zip(self.axes, self.ids, strict=True)}

    @property
    def cell(self) -> Cell:
        """Static axis values (what ``ctx.cell`` / ``DepCtx.cell`` expose)."""

        return Cell(
            {
                axis.name: value
                for axis, value in zip(self.axes, self.values, strict=True)
                if axis.is_static
            }
        )

    @property
    def relpath(self) -> str:
        return layout.unit_relpath([(axis.name, item) for axis, item in zip(self.axes, self.ids)])

    @property
    def unit_hash(self) -> str:
        return layout.unit_hash(self.unit_key)

    def axis_inputs(self) -> dict[str, PathSet]:
        """Path values of dynamic axes; they enter the ``inputs`` key segment."""

        return {
            axis.name: PathSet((value,), mode=axis.input_fingerprint)
            for axis, value in zip(self.axes, self.values, strict=True)
            if not axis.is_static and _is_path(value)
        }

    def axis_values(self) -> dict[str, Any]:
        """Non-path values of dynamic axes; they enter the ``values`` key segment."""

        return {
            axis.name: value
            for axis, value in zip(self.axes, self.values, strict=True)
            if not axis.is_static and not _is_path(value)
        }


def _is_path(value: Any) -> bool:
    return isinstance(value, Path)


def key_material(deps: Dependencies, unit: Unit) -> tuple[dict[str, Any], dict[str, Any]]:
    """Inputs and values of one unit's key: Dependencies plus dynamic axis paths/values."""

    inputs: dict[str, Any] = dict(deps.inputs)
    values: dict[str, Any] = dict(deps.values)
    for target, extra, kind in (
        (inputs, unit.axis_inputs(), "inputs"),
        (values, unit.axis_values(), "values"),
    ):
        clash = sorted(set(target) & set(extra))
        if clash:
            raise GraphError(
                f"Stage {unit.stage!r}: Dependencies {kind} {clash} clash with axis names"
            )
        target.update(extra)
    return inputs, values


DomainKey = tuple[Axis, tuple[str, ...]]
"""Dynamic domains are keyed by the axis and the ids of the static axes its resolver binds."""


@dataclass(frozen=True)
class DynamicRequest:
    """One resolver call needed to expand a stage: an axis within a resolver cell."""

    axis: Axis
    static_axes: tuple[Axis, ...]
    ids: tuple[str, ...]
    cell: Cell

    @property
    def key(self) -> DomainKey:
        return (self.axis, self.ids)


def static_domains(
    graph: StageGraph, active: Mapping[str, Sequence[str]] | None = None
) -> dict[Axis, AxisDomain]:
    """Static axis domains, restricted by a branch's active ids per axis name."""

    domains = {axis: axis.static_domain() for axis in graph.axes.values() if axis.is_static}
    for name, ids in (active or {}).items():
        axis = graph.axes.get(name)
        if axis is None or not axis.is_static:
            known = sorted(item.name for item in domains)
            raise GraphError(f"Unknown static axis {name!r}; static axes: {known}")
        domains[axis] = domains[axis].restrict(ids)
    return domains


def dynamic_requests(
    graph: StageGraph, node: StageNode, statics: Mapping[Axis, AxisDomain]
) -> list[DynamicRequest]:
    """Resolver calls needed to expand ``node``, one per (axis, bound static ids)."""

    requests: list[DynamicRequest] = []
    for axis in node.dynamic_axes:
        binding = graph.resolvers[axis]
        for ids, values in _product([statics[item] for item in binding.static_axes]):
            cell = Cell(
                {item.name: value for item, value in zip(binding.static_axes, values, strict=True)}
            )
            requests.append(DynamicRequest(axis, binding.static_axes, ids, cell))
    return requests


def _product(domains: Sequence[AxisDomain]) -> list[tuple[tuple[str, ...], tuple[Any, ...]]]:
    rows: list[tuple[tuple[str, ...], tuple[Any, ...]]] = [((), ())]
    for domain in domains:
        rows = [
            (ids + (item,), values + (value,))
            for ids, values in rows
            for item, value in zip(domain.ids, domain.values, strict=True)
        ]
    return rows


def expand_stage(
    graph: StageGraph,
    node: StageNode,
    statics: Mapping[Axis, AxisDomain],
    dynamics: Mapping[DomainKey, AxisDomain] | None = None,
) -> list[Unit]:
    """Expand ``node`` into units in foreach product order.

    ``dynamics`` must hold a domain for every ``dynamic_requests`` key;
    a missing one raises ``KeyError``.
    """

    dynamics = dynamics or {}
    axes = node.axes
    bound_positions: list[tuple[int, ...] | None] = []
    for axis in axes:
        if axis.is_static:
            bound_positions.append(None)
        else:
            binding = graph.resolvers[axis]
            bound_positions.append(tuple(axes.index(item) for item in binding.static_axes))

    rows: list[tuple[tuple[str, ...], tuple[Any, ...], tuple[Any, ...]]] = [((), (), ())]
    for axis, positions in zip(axes, bound_positions, strict=True):
        next_rows = []
        for ids, keys, values in rows:
            if positions is None:
                domain = statics[axis]
            else:
                lookup = (axis, tuple(ids[p] for p in positions))
                try:
                    domain = dynamics[lookup]
                except KeyError:
                    bound = graph.resolvers[axis].static_axes
                    cell = {item.name: ids[p] for item, p in zip(bound, positions, strict=True)}
                    raise KeyError(
                        f"Axis {axis.name!r} of stage {node.name!r} is not resolved for {cell}"
                    ) from None
            for item, key, value in zip(domain.ids, domain.keys, domain.values, strict=True):
                next_rows.append((ids + (item,), keys + (key,), values + (value,)))
        rows = next_rows

    names = [axis.name for axis in axes]
    return [
        Unit(
            stage=node.name,
            axes=axes,
            ids=ids,
            keys=keys,
            values=values,
            unit_key=",".join(f"{name}={item}" for name, item in zip(names, ids, strict=True)),
        )
        for ids, keys, values in rows
    ]


# ---------------------------------------------------------------------------
# Alignment

Upstream = Unit | dict[Any, Unit]
"""1-to-1 alignment yields a Unit; fan-in yields an ordered dict keyed by the upstream-only axes."""


def is_fan_in(down_axes: Sequence[Axis], up_axes: Sequence[Axis]) -> bool:
    return any(axis not in down_axes for axis in up_axes)


def align(
    down_axes: Sequence[Axis],
    down_ids: Sequence[tuple[str, ...]],
    up_axes: Sequence[Axis],
    up_units: Sequence[Unit],
    *,
    upstream_name: str = "upstream",
) -> list[Upstream]:
    """Match each downstream coordinate with upstream units by shared axes (identity).

    Shared axes align 1-to-1; upstream-only axes fan in as a dict keyed by the
    axis key (scalar for one axis, tuple in upstream foreach order for
    several), in upstream unit order; downstream-only axes broadcast. Fan-in
    dicts are shared between downstream coordinates with the same shared ids:
    treat them as read-only.
    """

    shared = [axis for axis in up_axes if axis in down_axes]
    down_pos = [list(down_axes).index(axis) for axis in shared]
    up_pos = [list(up_axes).index(axis) for axis in shared]
    rest = [index for index, axis in enumerate(up_axes) if axis not in shared]

    if not rest:
        index = {tuple(unit.ids[p] for p in up_pos): unit for unit in up_units}
        result: list[Upstream] = []
        for ids in down_ids:
            match = index.get(tuple(ids[p] for p in down_pos))
            if match is None:
                coords = {axis.name: ids[p] for axis, p in zip(shared, down_pos, strict=True)}
                raise AlignmentError(f"No unit of {upstream_name!r} matches {coords}")
            result.append(match)
        return result

    buckets: dict[tuple[str, ...], dict[Any, Unit]] = defaultdict(dict)
    single = len(rest) == 1
    for unit in up_units:
        key = unit.keys[rest[0]] if single else tuple(unit.keys[i] for i in rest)
        buckets[tuple(unit.ids[p] for p in up_pos)][key] = unit
    empty: dict[Any, Unit] = {}
    return [buckets.get(tuple(ids[p] for p in down_pos), empty) for ids in down_ids]


def align_units(
    down: StageNode, down_units: Sequence[Unit], up: StageNode, up_units: Sequence[Unit]
) -> list[Upstream]:
    """Align a stage's units with an upstream stage's units."""

    return align(
        down.axes, [unit.ids for unit in down_units], up.axes, up_units, upstream_name=up.name
    )


def align_request(request: DynamicRequest, up: StageNode, up_units: Sequence[Unit]) -> Upstream:
    """Align a resolver cell with an upstream stage's units."""

    return align(request.static_axes, [request.ids], up.axes, up_units, upstream_name=up.name)[0]


# ---------------------------------------------------------------------------
# Publish targets


@dataclass(frozen=True)
class PublishTarget:
    unit: Unit
    entry: PublishEntry
    dst: str
    """Path under the branch publish dir; for glob entries a pattern prefix (``<dir>/<glob>``),
    for directory targets the directory (ending in ``/``)."""


def publish_targets(node: StageNode, units: Sequence[Unit]) -> list[PublishTarget]:
    """Resolve every publish entry for every unit and reject colliding literal targets."""

    targets: list[PublishTarget] = []
    claimed: dict[str, str] = {}
    for unit in units:
        coords = unit.coords
        prefix = unit.relpath
        for entry in node.spec.publish:
            if entry.templated:
                dst = entry.dst.format(**coords)
            else:
                dst = f"{prefix}/{entry.dst}" if prefix else entry.dst
            targets.append(PublishTarget(unit, entry, dst))
            if entry.into_dir or (is_glob(entry.src) and not entry.templated):
                continue
            owner = f"{unit.unit_key or node.name}:{entry.src}"
            previous = claimed.setdefault(dst, owner)
            if previous != owner:
                raise GraphError(
                    f"Stage {node.name!r}: publish target {dst!r} is claimed by both "
                    f"{previous} and {owner}"
                )
    return targets


def publish_claims(node: StageNode, units: Sequence[Unit]) -> dict[str, str]:
    """Concrete non-glob publish destinations of ``units`` -> owner description."""

    claims: dict[str, str] = {}
    for target in publish_targets(node, units):
        entry = target.entry
        if entry.into_dir or (is_glob(entry.src) and not entry.templated):
            continue
        claims[target.dst] = f"{node.name}:{target.unit.unit_key or '-'}:{target.entry.src}"
    return claims


def check_publish_collisions(
    node: StageNode,
    units: Sequence[Unit],
    others: Iterable[tuple[StageNode, Sequence[Unit]]],
    cache: dict[tuple[str, int], dict[str, str]] | None = None,
) -> None:
    """Reject concrete publish destinations of ``node`` that another stage also claims.

    ``cache`` (keyed by stage name and the identity of its unit list) lets a run
    compute each stage's claims once instead of once per later publishing stage.
    """

    if not node.spec.publish:
        return

    def claims(stage: StageNode, stage_units: Sequence[Unit]) -> dict[str, str]:
        if cache is None:
            return publish_claims(stage, stage_units)
        key = (stage.name, id(stage_units))
        if key not in cache:
            cache[key] = publish_claims(stage, stage_units)
        return cache[key]

    mine = claims(node, units)
    for other, other_units in others:
        if other.name == node.name or not other.spec.publish or not other_units:
            continue
        theirs = claims(other, other_units)
        clash = sorted(mine.keys() & theirs.keys())
        if clash:
            dst = clash[0]
            raise GraphError(
                f"publish target {dst!r} is claimed by both {theirs[dst]} and {mine[dst]}"
            )
