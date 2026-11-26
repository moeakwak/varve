"""``publish=``: hardlink managed outputs into the branch publish dir, atomically.

Publishing runs after every unit of a stage is ``ok`` (also when all were
hits, so a deleted published file is restored). Each target is hardlinked to
a temporary name next to it and moved into place with ``os.replace``;
directories are rebuilt as a hardlinked tree and swapped in. The publish dir
must share a filesystem with the stage's storage: varve never copies.

Files published by glob entries are recorded per stage in
``<publish_dir>/.varve-published/<stage>.json`` (``[unit_key, src, [paths]]``),
so a file a unit's glob published earlier but no longer produces is removed
at the next publish. Only files varve recorded are ever removed; files of
units or entries that disappeared are left alone.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from collections.abc import Sequence
from pathlib import Path

from varve.decorators import is_glob
from varve.engine.unit import link_tree, remove_tree
from varve.graph import PublishTarget, StageNode, Unit, publish_targets
from varve.keying.fingerprint import glob_match, walk_files


class PublishError(RuntimeError):
    pass


def device_of(path: Path) -> int:
    """``st_dev`` of ``path`` or of its nearest existing ancestor."""

    current = path
    while not current.exists():
        if current.parent == current:
            break
        current = current.parent
    return os.stat(current).st_dev


def check_same_filesystem(node: StageNode, storage_root: Path, publish_dir: Path) -> None:
    if device_of(storage_root) != device_of(publish_dir):
        raise PublishError(
            f"Stage {node.name!r}: publish dir {publish_dir} is on a different filesystem than "
            f"its storage {node.storage!r} ({storage_root}); varve publishes by hardlink and "
            "never copies. Set storage= on the stage to a storage on the publish filesystem."
        )


def publish_stage(
    node: StageNode,
    units: Sequence[Unit],
    unit_dirs: dict[str, Path],
    storage_root: Path,
    publish_dir: Path,
) -> list[Path]:
    """Publish every target of ``units``; returns the published paths."""

    if not node.spec.publish:
        return []
    check_same_filesystem(node, storage_root, publish_dir)
    published: list[Path] = []
    globbed: dict[tuple[str, str], list[str]] = {}
    for target in publish_targets(node, units):
        source_dir = unit_dirs[target.unit.unit_key]
        expanded = _expand(target, source_dir)
        if is_glob(target.entry.src):
            globbed[(target.unit.unit_key, target.entry.src)] = [rel for _, rel in expanded]
        for source, relpath in expanded:
            destination = publish_dir / relpath
            _place(source, destination)
            published.append(destination)
    if globbed:
        _prune_globbed(publish_dir, node.name, globbed, {str(path) for path in published})
    return published


def _record_path(publish_dir: Path, stage: str) -> Path:
    return publish_dir / ".varve-published" / f"{stage}.json"


def _prune_globbed(
    publish_dir: Path, stage: str, globbed: dict[tuple[str, str], list[str]], published: set[str]
) -> None:
    """Remove files a current glob target published before but not now; update the record."""

    record = _record_path(publish_dir, stage)
    previous: dict[tuple[str, str], list[str]] = {}
    try:
        for unit_key, src, paths in json.loads(record.read_text()):
            previous[(str(unit_key), str(src))] = [str(path) for path in paths]
    except (OSError, ValueError, TypeError):
        pass
    for key, paths in globbed.items():
        for rel in previous.get(key, ()):
            if rel in paths or os.path.isabs(rel) or ".." in Path(rel).parts:
                continue
            stale = publish_dir / rel
            if str(stale) in published:
                continue
            try:
                if not stat.S_ISREG(stale.lstat().st_mode):
                    continue
                stale.unlink()
            except (FileNotFoundError, NotADirectoryError):
                continue
            parent = stale.parent
            while parent != publish_dir and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
    merged = {**previous, **globbed}
    record.parent.mkdir(parents=True, exist_ok=True)
    temporary = record.with_name(f".{record.name}.{secrets.token_hex(4)}")
    rows = [[unit_key, src, paths] for (unit_key, src), paths in sorted(merged.items())]
    temporary.write_text(json.dumps(rows))
    os.replace(temporary, record)


def _expand(target: PublishTarget, source_dir: Path) -> list[tuple[Path, str]]:
    entry = target.entry
    if not is_glob(entry.src):
        source = source_dir / entry.src
        if not source.exists():
            raise PublishError(f"publish source {entry.src!r} does not exist in {source_dir}")
        return [(source, target.dst)]
    prefix = target.dst[: len(target.dst) - len(entry.src)]
    matches = [
        (path, f"{prefix}{relpath}")
        for relpath, path in walk_files(source_dir)
        if glob_match(relpath, entry.src)
    ]
    if not matches:
        raise PublishError(f"publish glob {entry.src!r} matched nothing in {source_dir}")
    return matches


def _place(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.varve-{secrets.token_hex(4)}")
    if source.is_dir():
        link_tree(source, temporary)
        previous = None
        if destination.exists():
            previous = destination.with_name(f".{destination.name}.old-{secrets.token_hex(4)}")
            os.rename(destination, previous)
        os.rename(temporary, destination)
        if previous is not None:
            remove_tree(previous)
        return
    if destination.exists() and os.path.samefile(source, destination):
        return
    os.link(source, temporary)
    os.replace(temporary, destination)
