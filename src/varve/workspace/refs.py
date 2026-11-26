"""Zero-import resolution of cross-pipeline ``Ref`` inputs.

The upstream store is opened read-only: the ``branches`` row, the
target stage's ``units``, and the ``storages`` table give each committed
unit's artifact path (``storages.root_path`` + ``artifact_relpath``) and
fingerprint (``artifact_fp``) without importing the upstream module or
re-hashing anything. Only committed records are checked ("ok is not
current"); whole-chain freshness is the top-level ``varve run``'s job.

The upstream unit set is the branch's *active* domain: rows whose static
coordinates fall outside the declared axis ids (``meta.graph_json``) restricted
by the branch's ``active_axes_json`` (``axes:``/``--slice``/``--sample``), or
whose dynamic coordinates fall outside an explicit dynamic ``axes:`` list, are
not part of it, and ``orphaned`` rows are ignored. Within the set, every unit a
downstream coordinate reads must be ``ok``: a ``failed`` or ``running`` unit is
an error naming the command to run, never silently left out of a fan-in.

Axes align by name (spec §4.6.1): axes the downstream coordinate shares with
the upstream stage align 1-to-1, upstream-only axes fan in as an ordered
``dict`` keyed by the upstream id (a tuple of ids for several axes, in the
upstream foreach order; ids stay strings because nothing is imported). Entries
follow the upstream foreach product order: declared id order for static axes,
id string order for dynamic axes (their resolver order is not stored), so the
injected dict and the key material never depend on store row order.
``align_by=()`` fans everything in, ``align_by={"down": "up"}`` renames,
``align_by=("a", ...)`` aligns only on the listed names. A multi-branch Ref
wraps each value in an outer ``dict`` keyed by branch name. ``selector``
(``"axis=id,..."``) filters the upstream units first.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from varve.axis import Axis
from varve.decorators import parse_selector
from varve.engine.refs import RefBinding, RefInfo, RefResolutionError
from varve.ref import Ref
from varve.store import layout
from varve.store.db import SchemaError, connect, load_graph, read_meta
from varve.workspace.config import WorkspaceSettings
from varve.workspace.discovery import Entry, discover, match_entries
from varve.workspace.modules import locate_module


@dataclass(frozen=True)
class UpstreamLocation:
    name: str
    root: Path
    run_module: str | None

    def hint(self, branch: str) -> str:
        commands = []
        if self.run_module is not None:
            commands.append(f"python -m {self.run_module} run --branch {branch}")
        commands.append(f"varve run {self.name}@{branch}")
        return " (or ".join(commands) + (")" if len(commands) > 1 else "")


@dataclass(frozen=True)
class UpstreamUnit:
    ids: tuple[str, ...]
    unit_key: str
    path: Path
    fp: str


@dataclass(frozen=True)
class UpstreamStage:
    location: UpstreamLocation
    branch: str
    stage: str
    axes: tuple[str, ...]
    units: tuple[UpstreamUnit, ...]
    """Committed (``ok``) units of the active domain in foreach order (see ``_unit_order``)."""
    others: Mapping[tuple[str, ...], str]
    """ids -> status of active-domain units that exist but are not ``ok`` (nor orphaned)."""
    last_commit_at: str | None
    git_commit: str | None
    git_dirty: bool | None

    def where(self) -> str:
        return f"{self.location.name}:{self.stage}@{self.branch}"


class WorkspaceRefResolver:
    """The default :class:`~varve.engine.refs.RefResolver` (one per command).

    Upstream stores are located through the workspace: a Pipeline class
    target uses its own default root (``VARVE_ROOT_BASE/<name>`` under
    relocation); a string target is matched (full name, module, or
    unambiguous dotted suffix) against ``varve.toml`` pipelines and the
    stores discovered under the scan root. Reads are cached for the
    resolver's lifetime.
    """

    def __init__(self, settings: WorkspaceSettings | None = None) -> None:
        self.settings = settings or WorkspaceSettings()
        self._entries: list[Entry] | None = None
        self._locations: dict[Any, UpstreamLocation] = {}
        self._stages: dict[tuple[Path, str, str], UpstreamStage] = {}
        self._bound: dict[str, tuple[Ref, list[UpstreamStage]]] = {}

    # -- RefResolver ---------------------------------------------------------

    def bind(
        self,
        name: str,
        ref: Ref,
        axes: Sequence[Axis],
        coords: Sequence[tuple[str, ...]],
    ) -> Sequence[RefBinding]:
        location = self.locate(ref, name)
        stages = [self.stage(location, branch, ref.stage, name) for branch in ref.branches]
        self._bound[name] = (ref, stages)
        if not coords:
            return []
        down = [axis.name for axis in axes]
        aligned = [_align(name, ref, stage, down, coords) for stage in stages]
        if not ref.multi_branch:
            return [RefBinding(value, key) for value, key in aligned[0]]
        return [
            RefBinding(
                {stage.branch: aligned[b][i][0] for b, stage in enumerate(stages)},
                {stage.branch: aligned[b][i][1] for b, stage in enumerate(stages)},
            )
            for i in range(len(coords))
        ]

    def bound_refs(self) -> list[RefInfo]:
        """Upstream records the bound Refs read (for ``status`` / ``inspect``)."""

        infos = []
        for attr, (ref, stages) in self._bound.items():
            for stage in stages:
                infos.append(
                    RefInfo(
                        attr=attr,
                        target=stage.location.name,
                        stage=ref.stage,
                        branch=stage.branch,
                        root=stage.location.root,
                        units=len(stage.units),
                        last_commit_at=stage.last_commit_at,
                        git_commit=stage.git_commit,
                        git_dirty=stage.git_dirty,
                    )
                )
        return infos

    # -- location ------------------------------------------------------------

    def entries(self) -> list[Entry]:
        if self._entries is None:
            self._entries = discover(self.settings)
        return self._entries

    def locate(self, ref: Ref, name: str = "?") -> UpstreamLocation:
        target = ref.target
        cached = self._locations.get(target)
        if cached is not None:
            return cached
        location = (
            self._locate_name(target, name)
            if isinstance(target, str)
            else self._locate_class(target)
        )
        self._locations[target] = location
        return location

    def _locate_class(self, pipeline: Any) -> UpstreamLocation:
        from varve.engine.session import pipeline_root

        name = pipeline.selector()
        root = pipeline_root(pipeline, None, self.settings)
        main = locate_module(name)
        run_module = name if main is not None and main.has_main else pipeline.import_module_name()
        return UpstreamLocation(name, root, run_module)

    def _locate_name(self, target: str, attr: str) -> UpstreamLocation:
        matches = match_entries(self.entries(), target)
        if len(matches) == 1:
            entry = matches[0]
            if entry.root is None:
                raise RefResolutionError(
                    f"Ref {attr!r}: cannot locate upstream {target!r} ({entry.error})"
                )
            return UpstreamLocation(entry.name, entry.root, entry.run_module())
        if len(matches) > 1:
            details = "; ".join(f"{entry.name} ({entry.root})" for entry in matches)
            raise RefResolutionError(
                f"Ref {attr!r}: upstream {target!r} is ambiguous: {details}. "
                "Use the full pipeline name or a Pipeline class target."
            )
        base = self.settings.root_base
        if base is not None:
            return UpstreamLocation(target, base / target, None)
        where = self.settings.scan_root
        raise RefResolutionError(
            f"Ref {attr!r}: upstream pipeline {target!r} not found (no varve.toml entry and no "
            f"store under {where} matches). Register it in varve.toml or run it once: "
            f"varve run {target}"
        )

    # -- reading -------------------------------------------------------------

    def stage(
        self, location: UpstreamLocation, branch: str, stage: str, attr: str = "?"
    ) -> UpstreamStage:
        key = (location.root, branch, stage)
        cached = self._stages.get(key)
        if cached is None:
            cached = _read_stage(location, branch, stage, attr)
            self._stages[key] = cached
        return cached


def _read_stage(location: UpstreamLocation, branch: str, stage: str, attr: str) -> UpstreamStage:
    where = f"{location.name}:{stage}@{branch}"
    db_path = layout.state_db_path(location.root)
    if not db_path.is_file():
        raise RefResolutionError(
            f"Ref {attr!r}: upstream {location.name!r} has no store at {db_path}; "
            f"run it first: {location.hint(branch)}"
        )
    try:
        conn = connect(db_path, readonly=True)
    except (SchemaError, sqlite3.Error) as error:
        raise RefResolutionError(f"Ref {attr!r}: cannot read {db_path}: {error}") from error
    try:
        found = conn.execute(
            "SELECT active_axes_json FROM branches WHERE branch_id = ?", (branch,)
        ).fetchone()
        if found is None:
            names = [row[0] for row in conn.execute("SELECT branch_id FROM branches ORDER BY 1")]
            raise RefResolutionError(
                f"Ref {attr!r}: upstream {location.name!r} has no branch {branch!r} "
                f"(branches: {', '.join(names) or 'none'}); run it first: {location.hint(branch)}"
            )
        graph = load_graph(read_meta(conn))
        specs = {str(spec.get("name")): spec for spec in graph["stages"]} if graph else {}
        axis_specs = None
        if stage in specs:
            axis_specs = [axis for axis in specs[stage].get("axes", ()) if isinstance(axis, dict)]
        declared = None if axis_specs is None else tuple(str(a["name"]) for a in axis_specs)
        storages = {
            row[0]: Path(row[1])
            for row in conn.execute("SELECT storage_name, root_path FROM storages")
        }
        rows = conn.execute(
            "SELECT unit_key, status, storage_name, artifact_relpath, artifact_fp, updated_at, "
            "last_run_id FROM units WHERE branch_id = ? AND stage_name = ? ORDER BY rowid",
            (branch, stage),
        ).fetchall()
        if declared is None and not rows:
            if graph is not None:
                raise RefResolutionError(
                    f"Ref {attr!r}: upstream {location.name!r} has no stage {stage!r} "
                    f"(stages: {', '.join(specs)})"
                )
            raise RefResolutionError(
                f"Ref {attr!r}: {where} has no committed units; run it first: "
                f"{location.hint(branch)}"
            )
        axes = declared
        if axes is None:  # a stage no longer declared: its rows' axes
            axes = tuple(name for name, _ in layout.parse_unit_key(rows[0]["unit_key"]))
        allowed = _active_ids(axis_specs or (), json.loads(found[0]))
        allowed_at = [(axes.index(name), ids) for name, ids in allowed.items() if name in axes]
        units: list[UpstreamUnit] = []
        others: dict[tuple[str, ...], str] = {}
        latest: sqlite3.Row | None = None
        for row in rows:
            pairs = layout.parse_unit_key(row["unit_key"])
            if tuple(name for name, _ in pairs) != axes:  # an older foreach declaration
                continue
            ids = tuple(item for _, item in pairs)
            if any(ids[position] not in keep for position, keep in allowed_at):
                continue  # outside the branch's active domain
            if row["status"] != "ok" or not row["artifact_relpath"] or not row["artifact_fp"]:
                if row["status"] != "orphaned":
                    others[ids] = row["status"]
                continue
            storage_root = storages.get(row["storage_name"])
            if storage_root is None:
                raise RefResolutionError(
                    f"Ref {attr!r}: {where} unit {row['unit_key']!r} uses unknown storage "
                    f"{row['storage_name']!r}"
                )
            units.append(
                UpstreamUnit(
                    ids, row["unit_key"], storage_root / row["artifact_relpath"], row["artifact_fp"]
                )
            )
            if latest is None or row["updated_at"] > latest["updated_at"]:
                latest = row
        units.sort(key=_unit_order(axes, axis_specs or ()))
        git_commit = git_dirty = None
        if latest is not None and latest["last_run_id"]:
            run = conn.execute(
                "SELECT git_commit, git_dirty FROM runs WHERE run_id = ?", (latest["last_run_id"],)
            ).fetchone()
            if run is not None:
                git_commit = run["git_commit"]
                git_dirty = None if run["git_dirty"] is None else bool(run["git_dirty"])
        return UpstreamStage(
            location=location,
            branch=branch,
            stage=stage,
            axes=axes,
            units=tuple(units),
            others=others,
            last_commit_at=latest["updated_at"] if latest is not None else None,
            git_commit=git_commit,
            git_dirty=git_dirty,
        )
    finally:
        conn.close()


def _active_ids(
    axis_specs: Sequence[Mapping[str, Any]], active: Mapping[str, Any]
) -> dict[str, set[str]]:
    """Axis name -> ids of the branch's active domain, for axes whose domain is known.

    Static axes: the declared ids (stores written before ids were recorded
    skip the check) restricted by an ``axes:``/``--slice`` list, then sampled
    like the engine does. Dynamic axes: only an explicit id list restricts.
    """

    from varve.branch_config import Sample

    allowed: dict[str, set[str]] = {}
    for spec in axis_specs:
        name = str(spec.get("name"))
        restriction = active.get(name)
        listed = restriction
        if isinstance(restriction, Mapping):  # a sample, drawn from "ids" when present
            listed = restriction.get("ids")
        if spec.get("kind") == "static" and isinstance(spec.get("ids"), list):
            ids = [str(item) for item in spec["ids"]]
            if isinstance(listed, list):
                wanted = {str(item) for item in listed}
                ids = [item for item in ids if item in wanted]
            if isinstance(restriction, Mapping) and "sample" in restriction:
                sample = Sample(int(restriction["sample"]), int(restriction.get("seed", 0)))
                ids = sample.pick(ids, name)
            allowed[name] = set(ids)
        elif isinstance(listed, list):
            allowed[name] = {str(item) for item in listed}
    return allowed


def _unit_order(axes: tuple[str, ...], axis_specs: Sequence[Mapping[str, Any]]) -> Any:
    """Sort key giving upstream units a deterministic order, independent of store row order.

    Units are ordered like a foreach product over ``axes``: a static axis by its
    declared id order (``meta.graph_json``), a dynamic axis (whose resolver order
    is not recorded) by id string. Ids missing from a declared list sort last.
    """

    declared: dict[str, dict[str, int]] = {}
    for spec in axis_specs:
        if spec.get("kind") == "static" and isinstance(spec.get("ids"), list):
            declared[str(spec.get("name"))] = {str(item): i for i, item in enumerate(spec["ids"])}
    positions = [declared.get(name) for name in axes]

    def key(unit: UpstreamUnit) -> tuple[tuple[int, str], ...]:
        parts = []
        for order, item in zip(positions, unit.ids, strict=True):
            if order is None:
                parts.append((0, item))
            else:
                parts.append((order[item], "") if item in order else (len(order), item))
        return tuple(parts)

    return key


def _pairs(ref: Ref, attr: str, up: UpstreamStage, down: Sequence[str]) -> list[tuple[str, str]]:
    """(downstream axis, upstream axis) pairs that align 1-to-1."""

    align_by = ref.align_by
    if align_by is None:
        return [(name, name) for name in down if name in up.axes]
    pairs = (
        list(align_by.items())
        if isinstance(align_by, Mapping)
        else [(name, name) for name in align_by]
    )
    for down_name, up_name in pairs:
        if down_name not in down:
            raise RefResolutionError(
                f"Ref {attr!r}: align_by names downstream axis {down_name!r}, but the "
                f"consuming stage's axes are {list(down)}"
            )
        if up_name not in up.axes:
            raise RefResolutionError(
                f"Ref {attr!r}: align_by names upstream axis {up_name!r}, but "
                f"{up.where()} has axes {list(up.axes)}"
            )
    return pairs


def _align(
    attr: str,
    ref: Ref,
    up: UpstreamStage,
    down: Sequence[str],
    coords: Sequence[tuple[str, ...]],
) -> list[tuple[Any, Any]]:
    """(injected value, key entry) per downstream coordinate."""

    units: Sequence[UpstreamUnit] = up.units
    others: Mapping[tuple[str, ...], str] = up.others
    if ref.selector:
        wanted = parse_selector(ref.selector)
        unknown = [name for name, _ in wanted if name not in up.axes]
        if unknown:
            raise RefResolutionError(
                f"Ref {attr!r}: selector {ref.selector!r} names axes {unknown} that "
                f"{up.where()} does not have (axes: {list(up.axes)})"
            )
        positions = [(up.axes.index(name), item) for name, item in wanted]
        units = [unit for unit in units if all(unit.ids[p] == item for p, item in positions)]
        others = {
            ids: status
            for ids, status in others.items()
            if all(ids[p] == item for p, item in positions)
        }
    pairs = _pairs(ref, attr, up, down)
    down_pos = [list(down).index(d) for d, _ in pairs]
    up_pos = [up.axes.index(u) for _, u in pairs]
    rest = [i for i, name in enumerate(up.axes) if i not in up_pos]

    not_ok: dict[tuple[str, ...], list[tuple[tuple[str, ...], str]]] = {}
    for other_ids, state in others.items():
        not_ok.setdefault(tuple(other_ids[p] for p in up_pos), []).append((other_ids, state))

    def statuses(ids: tuple[str, ...]) -> list[str]:
        return sorted({state for _, state in not_ok.get(ids, ())})

    def missing(ids: tuple[str, ...]) -> RefResolutionError:
        coords_text = ", ".join(f"{u}={item}" for (_, u), item in zip(pairs, ids, strict=True))
        coords_text = coords_text or "(no axes)"
        status = statuses(ids)
        why = f"its units there are {', '.join(status)}" if status else "nothing is committed there"
        return RefResolutionError(
            f"Ref {attr!r}: {up.where()} has no committed (ok) unit for {coords_text} ({why}); "
            f"run it first: {up.location.hint(up.branch)}"
        )

    results: list[tuple[Any, Any]] = []
    if not rest:
        index = {tuple(unit.ids[p] for p in up_pos): unit for unit in units}
        for item in coords:
            ids = tuple(item[p] for p in down_pos)
            unit = index.get(ids)
            if unit is None:
                raise missing(ids)
            results.append((_existing(attr, up, unit), unit.fp))
        return results

    single = len(rest) == 1
    buckets: dict[tuple[str, ...], list[UpstreamUnit]] = {}
    for unit in units:
        buckets.setdefault(tuple(unit.ids[p] for p in up_pos), []).append(unit)
    built: dict[tuple[str, ...], tuple[dict[Any, Path], list[list[str]]]] = {}
    for item in coords:
        ids = tuple(item[p] for p in down_pos)
        entry = built.get(ids)
        if entry is None:
            members = buckets.get(ids)
            if not members:
                raise missing(ids)
            bad = not_ok.get(ids)
            if bad:
                listed = ", ".join(
                    f"{','.join(f'{up.axes[i]}={other[i]}' for i in rest)} {state}"
                    for other, state in bad[:5]
                )
                more = f" and {len(bad) - 5} more" if len(bad) > 5 else ""
                raise RefResolutionError(
                    f"Ref {attr!r}: {up.where()} fan-in is incomplete: {len(bad)} unit(s) are "
                    f"not ok ({listed}{more}); run it first: {up.location.hint(up.branch)}"
                )
            value: dict[Any, Path] = {}
            key: list[list[str]] = []
            for unit in members:
                fan_key = unit.ids[rest[0]] if single else tuple(unit.ids[i] for i in rest)
                value[fan_key] = _existing(attr, up, unit)
                key.append([unit.unit_key, unit.fp])
            entry = built[ids] = (value, key)
        results.append(entry)
    return results


def _existing(attr: str, up: UpstreamStage, unit: UpstreamUnit) -> Path:
    if not os.path.lexists(unit.path):
        raise RefResolutionError(
            f"Ref {attr!r}: {up.where()} unit {unit.unit_key or '(single)'} is recorded ok but "
            f"its artifact {unit.path} is missing; rerun it: {up.location.hint(up.branch)}"
        )
    return unit.path
