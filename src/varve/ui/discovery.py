"""Find ``.varve/state.db`` files under a directory without importing anything."""

from __future__ import annotations

import os
from pathlib import Path

#: Directory names never descended into (besides every other dot-directory).
SKIP_DIRS = frozenset({"node_modules", "__pycache__", "site-packages"})


def discover_state_dbs(root: Path) -> list[Path]:
    """Return every ``<dir>/.varve/state.db`` below ``root``, sorted.

    Skips dot-directories (``.git``, ``.venv``, ``.cache``, ``.worktrees``,
    ...), ``node_modules`` and similar, does not follow symlinks, and does not
    descend into varve storage roots (directories holding ``.internal``),
    which can contain thousands of unit directories.
    """

    found: list[Path] = []
    stack = [Path(root)]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        names = {entry.name for entry in entries}
        if ".varve" in names:
            candidate = current / ".varve" / "state.db"
            if candidate.is_file():
                found.append(candidate)
        for entry in entries:
            name = entry.name
            if name.startswith(".") or name in SKIP_DIRS:
                continue
            try:
                if not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            path = Path(entry.path)
            if (path / ".internal").is_dir():
                continue
            stack.append(path)
    return sorted(found)
