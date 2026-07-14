"""Matrix axes and branch-scoped stage graph expansion."""

from __future__ import annotations

import inspect
import re
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from difflib import get_close_matches
from enum import Enum
from graphlib import TopologicalSorter
from itertools import product
from pathlib import Path
from types import MappingProxyType
from typing import Any

from varve.decorators import StageSpec

_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _axis_id(value: str | int | Enum) -> str:
    if isinstance(value, Enum):
        result = value.value if isinstance(value, str) else value.name
    elif isinstance(value, str):
        result = value
    elif isinstance(value, int):
        result = str(value)
    else:
        raise TypeError("Axis values must be str, int, or Enum members")
    if not _ID_RE.fullmatch(result):
        raise ValueError(f"Invalid axis value id {result!r}; ids must match [A-Za-z0-9._-]+")
    return result


class Axis:
    """A reusable, ordered set of scalar matrix coordinates."""

    def __init__(self, name: str, values: Sequence[str | int | Enum]) -> None:
        if not _ID_RE.fullmatch(name):
            raise ValueError(f"Invalid axis name {name!r}; names must match [A-Za-z0-9._-]+")
        if not values:
            raise ValueError(f"Axis {name!r} must declare at least one value")
        ids = tuple(_axis_id(value) for value in values)
        if len(set(ids)) != len(ids):
            raise ValueError(f"Axis {name!r} has duplicate canonical ids")
        self.name = name
        self.values = tuple(values)
        self.ids = ids
        self._by_id = dict(zip(ids, self.values, strict=True))

    def value_for_id(self, value_id: str) -> str | int | Enum:
        try:
            return self._by_id[value_id]
        except KeyError as error:
            raise ValueError(
                f"Unknown value {value_id!r} for axis {self.name!r}; expected one of {list(self.ids)!r}"
            ) from error

    def id_of(self, value: str | int | Enum) -> str:
        return _axis_id(value)

    def __repr__(self) -> str:
        return f"Axis({self.name!r}, {list(self.values)!r})"


class Cell(Mapping[str, Any]):
    """Read-only coordinate mapping available as ``ctx.cell``."""

    def __init__(self, items: tuple[tuple[Axis, Any], ...] = ()) -> None:
        self._values = MappingProxyType({axis.name: value for axis, value in items})

    def __getitem__(self, name: str) -> Any:
        return self._values[name]

    def __iter__(self):
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError as error:
            raise AttributeError(name) from error


@dataclass(frozen=True)
class ResolvedStageSelector:
    """One validated selector and its active concrete stage seeds."""

    text: str
    canonical: str
    base_stage: str
    coordinates: tuple[tuple[str, str], ...]
    concrete_stages: tuple[str, ...]

    @property
    def is_concrete(self) -> bool:
        return len(self.concrete_stages) == 1 and self.canonical == self.concrete_stages[0]

    @property
    def matched_count(self) -> int:
        return len(self.concrete_stages)


@dataclass(frozen=True)
class PipelineGraph:
    """An immutable concrete stage graph expanded for one branch axis domain."""

    pipeline: type[Any]
    stages: Mapping[str, StageSpec]
    base_cells: Mapping[str, tuple[str, ...]]
    axes: Mapping[str, tuple[str, ...]]

    def topo_order(self) -> list[str]:
        dependencies = {name: set(spec.needs) for name, spec in self.stages.items()}
        return list(TopologicalSorter(dependencies).static_order())

    def names_for(self, name: str) -> tuple[str, ...]:
        return self.resolve_selector(name).concrete_stages

    def resolve_selector(self, text: str) -> ResolvedStageSelector:
        """Resolve an ordinary, Matrix base, partial, or concrete selector."""

        if not text or text.count("@") > 1:
            raise ValueError(f"Invalid stage selector {text!r}")
        base_stage, separator, raw_coordinates = text.partition("@")
        templates = self.pipeline.stages()
        template = templates.get(base_stage)
        if template is None:
            suggestions = get_close_matches(base_stage, templates, n=1)
            hint = f"; did you mean {suggestions[0]!r}?" if suggestions else ""
            raise ValueError(f"Unknown varve stage: {base_stage!r}{hint}")

        declared_axes = tuple(template.matrix)
        if separator and not declared_axes:
            raise ValueError(f"Ordinary stage {base_stage!r} does not accept coordinates")
        if separator and not raw_coordinates:
            raise ValueError(f"Invalid stage selector {text!r}: coordinates are empty")

        supplied: dict[str, str] = {}
        if separator:
            for coordinate in raw_coordinates.split(","):
                axis_name, equals, value_id = coordinate.partition("=")
                if not equals or not axis_name or not value_id or "=" in value_id:
                    raise ValueError(
                        f"Invalid stage selector {text!r}: expected AXIS=VALUE coordinates"
                    )
                if axis_name in supplied:
                    raise ValueError(f"Duplicate axis {axis_name!r} in stage selector {text!r}")
                supplied[axis_name] = value_id

        axes_by_name = {axis.name: axis for axis in declared_axes}
        unknown_axes = [name for name in supplied if name not in axes_by_name]
        if unknown_axes:
            raise ValueError(
                f"Unknown axis {unknown_axes[0]!r} for stage {base_stage!r}; "
                f"available axes: {list(axes_by_name)!r}"
            )
        for axis_name, value_id in supplied.items():
            axis = axes_by_name[axis_name]
            if value_id not in axis.ids:
                raise ValueError(
                    f"Unknown value {value_id!r} for axis {axis_name!r}; "
                    f"active values: {list(self.axes[axis_name])!r}"
                )
            if value_id not in self.axes[axis_name]:
                raise ValueError(
                    f"Value {value_id!r} for axis {axis_name!r} is declared but inactive; "
                    f"active values: {list(self.axes[axis_name])!r}"
                )

        coordinates = tuple(
            (axis.name, supplied[axis.name]) for axis in declared_axes if axis.name in supplied
        )
        canonical = base_stage
        if coordinates:
            canonical += "@" + ",".join(f"{name}={value}" for name, value in coordinates)
        matches = []
        for stage_name in self.base_cells[base_stage]:
            spec = self.stages[stage_name]
            cell = {axis.name: axis.id_of(value) for axis, value in spec.cell}
            if all(cell.get(axis_name) == value_id for axis_name, value_id in coordinates):
                matches.append(stage_name)
        if not matches:
            raise ValueError(f"Stage selector {canonical!r} matches no active cells")
        topology = {name: index for index, name in enumerate(self.topo_order())}
        matches.sort(key=topology.__getitem__)
        return ResolvedStageSelector(
            text=text,
            canonical=canonical,
            base_stage=base_stage,
            coordinates=coordinates,
            concrete_stages=tuple(matches),
        )

    def resolve_selectors(self, texts: Sequence[str]) -> tuple[str, ...]:
        """Validate all selectors, then return their stable topology-order union."""

        resolved = tuple(self.resolve_selector(text) for text in texts)
        selected = {stage for selector in resolved for stage in selector.concrete_stages}
        return tuple(name for name in self.topo_order() if name in selected)

    def closure(self, seeds: Iterable[str], *, downstream: bool = False) -> set[str]:
        if downstream:
            edges = {name: set() for name in self.stages}
            for name, spec in self.stages.items():
                for upstream in spec.needs:
                    edges[upstream].add(name)
        else:
            edges = {name: set(spec.needs) for name, spec in self.stages.items()}
        seen: set[str] = set()
        stack = list(seeds)
        while stack:
            item = stack.pop()
            if item not in seen:
                seen.add(item)
                stack.extend(edges[item])
        return seen

    def selected(
        self,
        *,
        upto: str | None = None,
        downstream: str | None = None,
        only: str | None = None,
        slices: Sequence[str] = (),
    ) -> set[str]:
        if sum(item is not None for item in (upto, downstream, only)) > 1:
            raise ValueError("only, upto, and downstream are mutually exclusive")
        if only is not None:
            selected = set(self.resolve_selector(only).concrete_stages)
        elif downstream is not None:
            selected = self.closure(
                self.resolve_selector(downstream).concrete_stages, downstream=True
            )
        elif upto is not None:
            selected = self.closure(self.resolve_selector(upto).concrete_stages)
        else:
            selected = set(self.stages)
        if not slices:
            return selected

        wanted: dict[str, set[str]] = defaultdict(set)
        for item in slices:
            axis_name, separator, value_id = item.partition("=")
            if not separator or axis_name not in self.axes or value_id not in self.axes[axis_name]:
                raise ValueError(f"Invalid matrix slice {item!r}")
            wanted[axis_name].add(value_id)
        seeds = []
        for name in selected:
            spec = self.stages[name]
            coordinates = {axis.name: axis.id_of(value) for axis, value in spec.cell}
            if coordinates and all(
                axis in coordinates and coordinates[axis] in ids for axis, ids in wanted.items()
            ):
                seeds.append(name)
        return self.closure(seeds)


def cell_output_path(output_root: Path, spec: StageSpec) -> Path:
    """Return the managed artifact root for a concrete stage."""

    if not spec.cell:
        return output_root
    if spec.base_name is None:
        raise ValueError(f"Matrix cell {spec.name!r} has no base stage metadata")
    path = output_root / ".matrix" / spec.base_name
    for axis, value in spec.cell:
        path /= f"{axis.name}={axis.id_of(value)}"
    return path


def pipeline_axes(pipeline: type[Any]) -> dict[str, Axis]:
    by_name: dict[str, Axis] = {}
    for spec in pipeline.stages().values():
        for axis in spec.matrix:
            if by_name.get(axis.name, axis) is not axis:
                raise ValueError(
                    f"Duplicate matrix axis name {axis.name!r} refers to different Axis objects"
                )
            by_name[axis.name] = axis
    return by_name


def normalize_axes(
    pipeline: type[Any], raw_axes: Mapping[str, Sequence[str]] | None
) -> dict[str, tuple[str, ...]]:
    declared = pipeline_axes(pipeline)
    raw_axes = raw_axes or {}
    unknown = sorted(set(raw_axes) - set(declared))
    if unknown:
        raise ValueError(f"Unknown matrix axes: {unknown!r}")
    result: dict[str, tuple[str, ...]] = {}
    for name, axis in declared.items():
        requested = tuple(raw_axes.get(name, axis.ids))
        if not requested:
            raise ValueError(f"Active domain for axis {name!r} must not be empty")
        if len(set(requested)) != len(requested):
            raise ValueError(f"Active domain for axis {name!r} contains duplicates")
        invalid = [value for value in requested if value not in axis.ids]
        if invalid:
            raise ValueError(f"Unknown values for axis {name!r}: {invalid!r}")
        requested_set = set(requested)
        result[name] = tuple(value_id for value_id in axis.ids if value_id in requested_set)
    return result


def build_graph(
    pipeline: type[Any], axes: Mapping[str, Sequence[str]] | None = None
) -> PipelineGraph:
    active = normalize_axes(pipeline, axes)
    templates = pipeline.stages()
    for template in templates.values():
        parameters = list(inspect.signature(template.func).parameters.values())[2:]
        coordinate_names = {axis.name for axis in template.matrix}
        actual_names = {parameter.name for parameter in parameters}
        if actual_names != coordinate_names or any(
            parameter.kind is not inspect.Parameter.KEYWORD_ONLY for parameter in parameters
        ):
            raise TypeError(
                f"Stage {template.name!r} coordinate parameters must be keyword-only and exactly "
                f"match its matrix axes {sorted(coordinate_names)!r}"
            )
    expanded: dict[str, list[StageSpec]] = {}
    for base_name, template in templates.items():
        values = [
            tuple(axis.value_for_id(value_id) for value_id in active[axis.name])
            for axis in template.matrix
        ]
        cells = product(*values) if values else [()]
        expanded[base_name] = []
        for coordinate_values in cells:
            cell = tuple(zip(template.matrix, coordinate_values, strict=True))
            coordinates = ",".join(f"{axis.name}={axis.id_of(value)}" for axis, value in cell)
            name = f"{template.name}@{coordinates}" if cell else template.name
            expanded[base_name].append(template.expanded(name=name, cell=cell))

    concrete: dict[str, StageSpec] = {}
    base_cells: dict[str, tuple[str, ...]] = {}
    for base_name, cells in expanded.items():
        resolved_cells: list[str] = []
        for cell in cells:
            cell_values = dict(cell.cell)
            actual_needs: list[str] = []
            need_cells: dict[str, tuple[str, ...]] = {}
            for logical_need in cell.logical_needs:
                matches = []
                for upstream in expanded[logical_need]:
                    upstream_values = dict(upstream.cell)
                    shared = set(cell_values) & set(upstream_values)
                    if all(cell_values[axis] == upstream_values[axis] for axis in shared):
                        matches.append(upstream.name)
                need_cells[logical_need] = tuple(matches)
                actual_needs.extend(matches)
            resolved = cell.with_wiring(tuple(actual_needs), need_cells)
            if resolved.name in concrete:
                raise ValueError(f"Duplicate varve stage: {resolved.name}")
            concrete[resolved.name] = resolved
            resolved_cells.append(resolved.name)
        base_cells[base_name] = tuple(resolved_cells)
    return PipelineGraph(
        pipeline=pipeline,
        stages=MappingProxyType(concrete),
        base_cells=MappingProxyType(base_cells),
        axes=MappingProxyType(active),
    )
