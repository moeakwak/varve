"""Run housekeeping: run ids, git state, startup recovery, log rotation, percentiles."""

from __future__ import annotations

import math
import os
import secrets
import shutil
import subprocess
from collections.abc import Collection, Sequence
from datetime import datetime
from pathlib import Path

from varve.engine.session import Session
from varve.engine.unit import remove_tree
from varve.store import layout

KEEP_RUN_LOGS = 20


def new_run_id() -> str:
    return f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{secrets.token_hex(3)}"


def git_state(cwd: Path) -> tuple[str | None, bool | None]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, timeout=5
        )
        if commit.returncode != 0:
            return None, None
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return commit.stdout.strip(), bool(dirty.stdout.strip()) if dirty.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None, None


def percentile(values: Sequence[int], fraction: float) -> int | None:
    """Nearest-rank percentile."""

    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def unit_pairs(unit_key: str) -> list[tuple[str, str]]:
    if not unit_key:
        return []
    return [(name, item) for name, _, item in (part.partition("=") for part in unit_key.split(","))]


def recover(session: Session, current_run: str) -> list[str]:
    """Finish what interrupted runs of this branch left behind; returns notes.

    Restores ``.old-*`` unit dirs of units still marked ``running`` (when the
    final dir is missing; otherwise drops the backup), removes the staging
    dirs of this branch's earlier runs, and marks their ``running`` runs (and
    the stages those runs left ``running``) ``interrupted``. Units stay
    ``running`` in the store: probes report them as ``needs-run · interrupted``.
    """

    repo = session.repo
    assert repo is not None
    notes: list[str] = []
    branch = session.branch.name
    for row in repo.units_with_status("running", branch_id=session.branch_id):
        root = session.storage_roots.get(row.storage_name)
        if root is None:
            continue
        final = layout.internal_dir(
            root, branch, row.stage_name, layout.unit_relpath(unit_pairs(row.unit_key))
        )
        if not final.parent.exists():
            continue
        for backup in sorted(final.parent.glob(f"{final.name}.old-*")):
            if final.exists():
                remove_tree(backup)
            else:
                os.rename(backup, final)
                notes.append(f"restored {row.stage_name}@{row.unit_key or '-'} from {backup.name}")
    run_ids = {
        run.run_id
        for run in repo.runs(session.branch_id, limit=1_000_000)
        if run.run_id != current_run
    }
    for root in session.storage_roots.values():
        staging = layout.staging_root(root)
        if not staging.is_dir():
            continue
        for entry in staging.iterdir():
            if entry.name in run_ids:
                remove_tree(entry)
                notes.append(f"removed leftover staging {entry}")
    for run in repo.runs(session.branch_id, limit=1_000_000):
        if run.status == "running" and run.run_id != current_run:
            repo.finish_run(run.run_id, status="interrupted", total_wall_ms=None)
    return notes


def rotate_logs(root: Path, keep: int = KEEP_RUN_LOGS, active: Collection[str] = ()) -> None:
    """Keep the logs of the ``keep`` newest runs, plus those of ``active`` runs.

    Runs of other branches may still be writing their logs (branches run
    concurrently), so the run ids still recorded as ``running`` are passed in
    ``active`` and never removed.
    """

    base = layout.logs_dir(root)
    if not base.is_dir():
        return
    runs = sorted(entry for entry in base.iterdir() if entry.is_dir() and entry.name not in active)
    for entry in runs[:-keep] if len(runs) > keep else []:
        shutil.rmtree(entry, ignore_errors=True)
