"""Matrix axes and branch-scoped stage graph expansion."""

from __future__ import annotations

import inspect
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
        if name in self.base_cells:
            return self.base_cells[name]
        if name in self.stages:
            return (name,)
        raise ValueError(f"Unknown varve stage: {name}")

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
        ancestors = {name: set(spec.needs) for name, spec in self.stages.items()}
        descendants = {name: set() for name in self.stages}
        for name, spec in self.stages.items():
            for upstream in spec.needs:
                descendants[upstream].add(name)

        def closure(seeds: Sequence[str], edges: Mapping[str, set[str]]) -> set[str]:
            seen: set[str] = set()
            stack = list(seeds)
            while stack:
                item = stack.pop()
                if item not in seen:
                    seen.add(item)
                    stack.extend(edges[item])
            return seen

        if only is not None:
            selected = set(self.names_for(only))
        elif downstream is not None:
            selected = closure(self.names_for(downstream), descendants)
        elif upto is not None:
            selected = closure(self.names_for(upto), ancestors)
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
        return closure(seeds, ancestors)


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
            previous = by_name.get(axis.name)
            if previous is not None and previous is not axis:
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


def _cell_name(spec: StageSpec, cell: tuple[tuple[Axis, Any], ...]) -> str:
    if not cell:
        return spec.name
    coordinates = ",".join(f"{axis.name}={axis.id_of(value)}" for axis, value in cell)
    return f"{spec.name}@{coordinates}"


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
            expanded[base_name].append(
                template.expanded(name=_cell_name(template, cell), cell=cell)
            )

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
