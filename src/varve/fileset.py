"""Helpers for declaring file sets in stage keys."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

RootSpec = Path | Callable[[Any], Path]


def file_set(
    *,
    root: RootSpec,
    include: Iterable[str],
    allow_empty: bool = False,
) -> Callable[[Any], list[Path]]:
    """Return a ``KeySpec.files`` resolver for globbed files under a root."""
    patterns = tuple(include)
    if not patterns:
        raise ValueError("file_set include must contain at least one glob pattern")

    def resolve(ctx: Any) -> list[Path]:
        root_path = root(ctx) if callable(root) else root
        root_path = root_path.expanduser().resolve()
        if not root_path.exists():
            raise FileNotFoundError(f"file_set root does not exist: {root_path}")
        if not root_path.is_dir():
            raise NotADirectoryError(f"file_set root is not a directory: {root_path}")

        paths: set[Path] = set()
        missing: list[str] = []
        for pattern in patterns:
            matches = [path.resolve() for path in root_path.glob(pattern) if path.is_file()]
            if not matches:
                missing.append(pattern)
            paths.update(matches)

        if missing and not allow_empty:
            listed = ", ".join(missing)
            raise FileNotFoundError(f"file_set pattern(s) matched no files under {root_path}: {listed}")

        return sorted(paths, key=str)

    return resolve
