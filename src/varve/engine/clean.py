"""``clean``: remove managed outputs and their store rows.

* whole pipeline (no filter): every storage's ``.internal``/``.staging``, the
  publish dirs of known branches, the state database and the run logs;
* ``branch``: that branch's managed dirs, publish dir, and rows;
* ``storage``: one storage's managed dirs and the rows stored there;
* ``orphans``: unit dirs and rows of orphaned units, then stale stat-cache rows.

Branch, storage, and orphan cleans also drop key parts, source details, and
file sets no remaining unit references. Storage roots are those recorded in the store's ``storages`` table (computed
roots only for names it does not record). Lock files are kept.
Whole/storage/orphan cleans hold the pipeline lock exclusively; a branch clean
holds it shared plus the branch lock.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from varve.branch import load_branches, validate_branch_name
from varve.engine.session import WorkspaceSettings, main_config, resolve_roots
from varve.engine.unit import remove_tree
from varve.pipeline import Pipeline
from varve.store import layout
from varve.store.db import connect
from varve.store.lock import branch_lock, pipeline_lock
from varve.store.repo import Repo
from varve.workspace.config import settings_for


@dataclass
class CleanPlan:
    paths: list[Path] = field(default_factory=list)
    rows: int = 0
    description: str = ""


class PipelineRoots(NamedTuple):
    root: Path
    storage_roots: dict[str, Path]
    publish_root: Path


def pipeline_roots(
    pipeline: type[Pipeline], root: Path | None, workspace: WorkspaceSettings | None = None
) -> PipelineRoots:
    """Roots under the ``main`` Config (what ``clean`` and ``ls`` address)."""

    workspace = workspace if workspace is not None else settings_for(pipeline)
    return PipelineRoots(*resolve_roots(pipeline, main_config(pipeline), root, workspace))


def _existing(paths: list[Path]) -> list[Path]:
    return [path for path in paths if path.exists() or path.is_symlink()]


def _known_branches(pipeline: type[Pipeline], repo: Repo | None) -> dict[str, bool]:
    branches = {
        name: item.is_temporary
        for name, item in load_branches(pipeline.varve_config_path()).items()
    }
    branches.setdefault("main", False)
    if repo is not None:
        branches.update({row.branch_id: row.is_temp for row in repo.list_branches()})
    return branches


def _open(roots: PipelineRoots) -> Repo | None:
    db_path = layout.state_db_path(roots.root)
    return Repo(connect(db_path)) if db_path.exists() else None


def _recorded(roots: PipelineRoots, repo: Repo | None) -> PipelineRoots:
    """``roots`` with the storage roots recorded in the store's ``storages`` table
    (where the managed files actually are); computed roots fill in unrecorded names."""

    if repo is None:
        return roots
    recorded = repo.storages()
    if not recorded:
        return roots
    return roots._replace(storage_roots={**roots.storage_roots, **recorded})


def plan_clean(
    pipeline: type[Pipeline],
    *,
    root: Path | None = None,
    branch: str | None = None,
    storage: str | None = None,
    orphans: bool = False,
    workspace: WorkspaceSettings | None = None,
) -> CleanPlan:
    """What :func:`clean_pipeline` would remove (paths and row count)."""

    return _clean(pipeline, root, branch, storage, orphans, workspace, apply=False)


def clean_pipeline(
    pipeline: type[Pipeline],
    *,
    root: Path | None = None,
    branch: str | None = None,
    storage: str | None = None,
    orphans: bool = False,
    workspace: WorkspaceSettings | None = None,
) -> CleanPlan:
    return _clean(pipeline, root, branch, storage, orphans, workspace, apply=True)


def _clean(
    pipeline: type[Pipeline],
    root: Path | None,
    branch: str | None,
    storage: str | None,
    orphans: bool,
    workspace: WorkspaceSettings | None,
    *,
    apply: bool,
) -> CleanPlan:
    roots = pipeline_roots(pipeline, root, workspace)
    if branch is not None:
        validate_branch_name(branch)
    if storage is not None and storage not in roots.storage_roots:
        raise ValueError(f"Unknown storage {storage!r}; storages: {sorted(roots.storage_roots)}")
    whole = branch is None and storage is None and not orphans
    exclusive = whole or storage is not None or orphans
    with pipeline_lock(roots.root, exclusive=exclusive):
        if whole:
            return _clean_whole(pipeline, roots, apply)
        repo = _open(roots)
        try:
            roots = _recorded(roots, repo)
            if orphans:
                return _clean_orphans(roots, repo, branch, apply)
            if storage is not None:
                return _clean_storage(roots, repo, storage, branch, apply)
            assert branch is not None
            with branch_lock(roots.root, branch):
                return _clean_branch(pipeline, roots, repo, branch, apply)
        finally:
            if repo is not None:
                repo.conn.close()


def _clean_whole(pipeline: type[Pipeline], roots: PipelineRoots, apply: bool) -> CleanPlan:
    try:
        repo = _open(roots)
    except Exception:  # an unreadable or foreign database is removed anyway
        repo = None
    branches = _known_branches(pipeline, repo)
    if repo is not None:
        try:
            roots = _recorded(roots, repo)
        except Exception:  # a foreign database without a usable storages table
            pass
        repo.conn.close()
    paths: list[Path] = []
    for storage_root in roots.storage_roots.values():
        paths += [storage_root / ".internal", storage_root / ".staging"]
    for name, temporary in branches.items():
        paths.append(layout.publish_dir(roots.publish_root, name, temporary=temporary))
    paths.append(roots.publish_root / ".tmp")
    db = layout.state_db_path(roots.root)
    paths += [db, db.with_name("state.db-wal"), db.with_name("state.db-shm")]
    paths.append(layout.logs_dir(roots.root))
    plan = CleanPlan(_existing(paths), description="the whole pipeline store")
    if apply:
        for path in plan.paths:
            remove_tree(path)
    return plan


def _clean_branch(
    pipeline: type[Pipeline], roots: PipelineRoots, repo: Repo | None, branch: str, apply: bool
) -> CleanPlan:
    temporary = _known_branches(pipeline, repo).get(branch, False)
    paths = [layout.internal_root(root, branch) for root in roots.storage_roots.values()]
    paths.append(layout.publish_dir(roots.publish_root, branch, temporary=temporary))
    if repo is not None and repo.get_branch(branch) is None:
        repo = None  # an unknown branch has no rows
    count = 0
    if repo is not None:
        count = sum(sum(by.values()) for by in repo.unit_counts(branch).values())
    plan = CleanPlan(_existing(paths), count, f"branch {branch!r}")
    if apply:
        for path in plan.paths:
            remove_tree(path)
        if repo is not None:
            repo.delete_branch(branch)
            repo.collect_garbage()
    return plan


def _clean_storage(
    roots: PipelineRoots, repo: Repo | None, storage: str, branch: str | None, apply: bool
) -> CleanPlan:
    storage_root = roots.storage_roots[storage]
    if branch is None:
        paths = [storage_root / ".internal", storage_root / ".staging"]
    else:
        paths = [layout.internal_root(storage_root, branch)]
    if repo is not None and branch is not None and repo.get_branch(branch) is None:
        repo = None  # unknown branch: no rows to count or delete (never every branch's)
    rows = repo.count_units_in_storage(storage, branch_id=branch) if repo is not None else 0
    plan = CleanPlan(_existing(paths), rows, f"storage {storage!r}")
    if apply:
        for path in plan.paths:
            remove_tree(path)
        if repo is not None:
            repo.delete_units_in_storage(storage, branch_id=branch)
            repo.collect_garbage()
    return plan


def _clean_orphans(
    roots: PipelineRoots, repo: Repo | None, branch: str | None, apply: bool
) -> CleanPlan:
    if repo is None:
        return CleanPlan(description="orphaned units")
    rows = repo.unit_keys_with_status("orphaned", branch_id=branch)
    paths = []
    for branch_id, stage, unit_key, storage in rows:
        storage_root = roots.storage_roots.get(storage)
        if storage_root is None:
            continue
        relpath = layout.unit_relpath(layout.parse_unit_key(unit_key))
        paths.append(layout.internal_dir(storage_root, branch_id, stage, relpath))
    plan = CleanPlan(_existing(paths), len(rows), "orphaned units")
    if apply:
        for path in plan.paths:
            remove_tree(path)
        with repo.transaction():
            for branch_id, stage, unit_key, _ in rows:
                repo.delete_units(branch_id, stage, [unit_key])
        repo.collect_garbage()
        repo.prune_stat_cache()
    return plan
