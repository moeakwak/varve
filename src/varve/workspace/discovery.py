"""Zero-import discovery of a workspace's pipelines and their stores.

Registered pipelines come from ``varve.toml``; stores are the
``.varve/state.db`` files found under the scan root (``VARVE_ROOT_BASE`` or the
workspace root) plus the expected roots of registered pipelines. Only
``meta`` and aggregate rows are read; no user module is imported.
"""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from varve.selector import matches_selector
from varve.store import layout
from varve.store.db import SchemaError, connect, read_meta
from varve.store.repo import Repo
from varve.ui.discovery import discover_state_dbs
from varve.workspace.config import WorkspaceSettings
from varve.workspace.modules import ModuleLocation, entry_selector, locate_module, split_entry

UNREGISTERED = 1 << 30


@dataclass(frozen=True)
class BranchSummary:
    name: str
    is_temp: bool
    manual: bool
    counts: dict[str, int]
    last_status: str | None = None
    last_started_at: str | None = None
    last_wall_ms: int | None = None


@dataclass
class Entry:
    """One pipeline known to the workspace (registered, discovered, or both)."""

    name: str
    """Selector: ``meta.pipeline_name`` once a store exists, else derived from the entry."""
    module: str | None
    """Pipeline module (``meta.module``), else the registered module."""
    qualname: str | None = None
    entry: str | None = None
    """The ``varve.toml`` entry, when registered."""
    order: int = UNREGISTERED
    """Position in ``varve.toml`` (tie-break of macro ordering)."""
    root: Path | None = None
    """Recorded root (``meta.root``) or the expected root of an uninitialized entry."""
    db_path: Path | None = None
    location: ModuleLocation | None = None
    error: str | None = None
    branches: list[BranchSummary] = field(default_factory=list)

    @property
    def initialized(self) -> bool:
        return self.db_path is not None

    @property
    def import_target(self) -> tuple[str, str | None] | None:
        """(module, qualname) to import the pipeline class from."""

        if self.entry is not None:
            return split_entry(self.entry)
        if self.module is not None:
            return self.module, self.qualname
        return None

    def run_module(self) -> str | None:
        """Module for a ``python -m <module> run`` hint."""

        if self.entry is not None:
            module, _ = split_entry(self.entry)
            if self.location is None or not self.location.is_package or self.location.has_main:
                return module
        if self.module is not None:
            location = locate_module(self.name)
            if location is not None and location.has_main:
                return self.name
            return self.module
        return None


@dataclass(frozen=True)
class StoreInfo:
    db_path: Path
    root: Path
    meta: dict[str, str]
    error: str | None
    branches: list[BranchSummary]


def read_store(db_path: Path, *, summaries: bool = False) -> StoreInfo:
    """Read ``meta`` (and optionally per-branch summaries) of one store, read-only."""

    root = db_path.parent.parent
    try:
        conn = connect(db_path, readonly=True)
    except (SchemaError, sqlite3.Error) as error:
        return StoreInfo(db_path, root, {}, str(error), [])
    try:
        meta = read_meta(conn)
        branches: list[BranchSummary] = []
        if summaries:
            repo = Repo(conn)
            for row in repo.list_branches():
                counts: dict[str, int] = {}
                for by_status in repo.unit_counts(row.branch_id).values():
                    for status, count in by_status.items():
                        counts[status] = counts.get(status, 0) + count
                runs = repo.runs(row.branch_id, limit=1)
                last = runs[0] if runs else None
                branches.append(
                    BranchSummary(
                        row.name,
                        row.is_temp,
                        row.manual,
                        counts,
                        last.status if last else None,
                        last.started_at if last else None,
                        last.total_wall_ms if last else None,
                    )
                )
        return StoreInfo(db_path, root, meta, None, branches)
    except sqlite3.Error as error:
        return StoreInfo(db_path, root, {}, str(error), [])
    finally:
        conn.close()


def registered_entries(settings: WorkspaceSettings) -> list[Entry]:
    entries = []
    for order, item in enumerate(settings.pipelines):
        module, qualname = split_entry(item)
        location = locate_module(module, _search_path(settings))
        name = entry_selector(item, location)
        if settings.root_base is not None:
            root: Path | None = settings.root_base / name
        else:
            root = location.directory if location is not None else None
        entries.append(
            Entry(
                name=name,
                module=module,
                qualname=qualname,
                entry=item,
                order=order,
                root=root,
                location=location,
                error=None if location is not None else f"module {module!r} not found",
            )
        )
    return entries


def _search_path(settings: WorkspaceSettings) -> list[str] | None:
    if settings.workspace_root is None:
        return None
    root = str(settings.workspace_root)
    return sys.path if root in sys.path else [root, *sys.path]


def discover(settings: WorkspaceSettings, *, summaries: bool = False) -> list[Entry]:
    """Registered pipelines (``varve.toml`` order) followed by other discovered stores."""

    entries = registered_entries(settings)
    db_paths: list[Path] = []
    for entry in entries:
        if entry.root is not None:
            candidate = layout.state_db_path(entry.root)
            if candidate.is_file():
                db_paths.append(candidate.absolute())
    scan = settings.scan_root
    if scan.is_dir():
        db_paths.extend(path.absolute() for path in discover_state_dbs(scan))
    seen: set[Path] = set()
    extra: list[Entry] = []
    for db_path in db_paths:
        if db_path in seen:
            continue
        seen.add(db_path)
        store = read_store(db_path, summaries=summaries)
        owner = _owner(entries, store)
        if owner is not None:
            owner.db_path = db_path
            owner.root = Path(store.meta.get("root") or store.root)
            owner.name = store.meta.get("pipeline_name") or owner.name
            owner.module = store.meta.get("module") or owner.module
            owner.qualname = store.meta.get("qualname") or owner.qualname
            owner.error = store.error
            owner.branches = store.branches
            continue
        meta = store.meta
        extra.append(
            Entry(
                name=meta.get("pipeline_name") or f"<{store.root}>",
                module=meta.get("module"),
                qualname=meta.get("qualname"),
                root=Path(meta.get("root") or store.root),
                db_path=db_path,
                error=store.error,
                branches=store.branches,
            )
        )
    extra.sort(key=lambda entry: (entry.name, str(entry.db_path)))
    return entries + extra


def _owner(entries: Sequence[Entry], store: StoreInfo) -> Entry | None:
    """The registered entry a store belongs to (same root, else same pipeline name)."""

    unclaimed = [entry for entry in entries if entry.db_path is None]
    for entry in unclaimed:
        if entry.root is not None and _same(entry.root, store.root):
            return entry
    name = store.meta.get("pipeline_name")
    module = store.meta.get("module")
    for entry in unclaimed:
        if name is not None and name == entry.name:
            return entry
        if module is not None and entry.module is not None and entry.entry is not None:
            package = entry.location is not None and entry.location.is_package
            if module == entry.module or (package and module.startswith(entry.module + ".")):
                return entry
    return None


def _same(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return left.absolute() == right.absolute()


class TargetError(ValueError):
    """A MODULE selector matches no pipeline or several."""


def match_entries(entries: Sequence[Entry], query: str) -> list[Entry]:
    """Selector rules: exact name, then exact module/entry, then unambiguous dotted suffix."""

    exact = [entry for entry in entries if entry.name == query]
    if exact:
        return exact
    modules = [
        entry
        for entry in entries
        if query in (entry.module, entry.entry)
        or (entry.entry is not None and split_entry(entry.entry)[0] == query)
    ]
    if modules:
        return modules
    return [
        entry
        for entry in entries
        if matches_selector(entry.name, query)
        or (entry.module is not None and matches_selector(entry.module, query))
    ]


def resolve_entry(entries: Sequence[Entry], query: str) -> Entry:
    matches = match_entries(entries, query)
    if len(matches) == 1:
        return matches[0]
    if not matches:
        available = ", ".join(sorted({entry.name for entry in entries})) or "(none)"
        raise TargetError(f"Unknown pipeline {query!r}. Known pipelines: {available}")
    details = "; ".join(f"{entry.name} ({entry.root})" for entry in matches)
    raise TargetError(
        f"Ambiguous pipeline {query!r}: {details}. "
        "Use the full name, or declare varve_name on a pipeline to make it distinct."
    )
