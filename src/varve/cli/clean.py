"""Destructive clean operations with conservative safety checks."""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from varve.pipeline import Pipeline
from varve.store.lock import OutputLock
from varve.store.store import Store


def default_confirm(message: str) -> bool:
    try:
        answer = input(f"{message} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in {"y", "yes"}


def _validate_destructive(root: Path, allowed_roots: list[Path] | None = None) -> None:
    resolved = root.expanduser().resolve()
    if str(root) == "":
        raise ValueError("Refusing to clean an empty path")
    dangerous = {Path("/").resolve(), Path.home().resolve(), Path.cwd().resolve()}
    if resolved in dangerous:
        raise ValueError(f"Refusing to clean dangerous path: {resolved}")
    if allowed_roots is not None and not any(
        resolved.is_relative_to(allowed.expanduser().resolve()) for allowed in allowed_roots
    ):
        raise ValueError(f"Refusing to clean path outside allowed roots: {resolved}")


def _confirm(message: str, yes: bool, confirm: Callable[[str], bool] | None) -> None:
    if not yes and (confirm is None or not confirm(message)):
        raise ValueError("Clean requires confirmation or yes=True")


def _read_manifest_anchor(store: Store, pipeline: type[Pipeline]) -> None:
    manifest_path = store.root / "manifest.json"
    manifest = store.read_manifest()
    if manifest is None:
        raise ValueError(f"Missing varve manifest anchor: {manifest_path}")
    if manifest.pipeline != pipeline.__name__:
        raise ValueError(f"Varve manifest belongs to {manifest.pipeline}, not {pipeline.__name__}")


def clean(
    pipeline: type[Pipeline],
    config: Any,
    *,
    cli_out: Path | None = None,
    branch: str = "main",
    is_temporary: bool = False,
    yes: bool = False,
    allowed_roots: list[Path] | None = None,
    confirm: Callable[[str], bool] | None = None,
) -> None:
    root = pipeline.output_root(
        config,
        cli_out=cli_out,
        branch=branch,
        is_temporary=is_temporary,
    )
    store = Store(root)
    with OutputLock(store.root):
        _read_manifest_anchor(store, pipeline)
        _validate_destructive(root, allowed_roots)
        _confirm(f"Clean full varve output root {root}?", yes, confirm)
        shutil.rmtree(root)
