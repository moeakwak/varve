"""File fingerprints: canonical JSON, input sets, artifact manifests, stat cache.

Declared roots (inputs, sources, storage roots) may sit behind symlinked path
prefixes; the trees expanded below a root must not contain symlinks (for a
glob, only the symlinks it would select or descend into count). Input
material never contains absolute paths, so moving a checkout or data
directory does not change keys: each declared root contributes its files'
paths relative to the root (a file root contributes its name).
"""

from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import stat as stat_module
import warnings
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from varve.dependencies import Glob, InputSpec, PathSet, as_input_set

_CHUNK = 1024 * 1024


def _normalize_for_json(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _normalize_for_json(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        bad = next((key for key in value if not isinstance(key, str)), None)
        if bad is not None:
            raise TypeError(f"JSON objects in varve keys need string keys; got {bad!r}")
        return {key: _normalize_for_json(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_normalize_for_json(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"NaN and Infinity are not allowed in varve keys, got {value!r}")
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if isinstance(value, Path):
        return value.as_posix()
    raise TypeError(f"Value is not JSON-serializable for varve keys: {type(value).__name__}")


def normalize_json(value: Any) -> Any:
    """Plain-JSON view of a key value (models dumped, tuples as lists, paths as POSIX)."""

    return _normalize_for_json(value)


def canonical_json(obj: Any) -> bytes:
    return json.dumps(
        _normalize_for_json(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def json_hash(obj: Any) -> str:
    """Hex sha256 of the canonical JSON of ``obj``."""

    return hashlib.sha256(canonical_json(obj)).hexdigest()


def _sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        file_digest = getattr(hashlib, "file_digest", None)  # Python >= 3.11
        if file_digest is not None:
            return file_digest(handle, "sha256").hexdigest()
        hasher = hashlib.sha256()
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            hasher.update(chunk)
        return hasher.hexdigest()


def _token(st: os.stat_result) -> tuple[int, int, int]:
    return st.st_ino, st.st_size, st.st_mtime_ns


def _stable(
    path: Path, read: Callable[[Path], Any], *, retries: int = 2
) -> tuple[Any, os.stat_result]:
    """Run ``read(path)`` until the file's stat token is unchanged across the read."""

    for _ in range(retries + 1):
        before = path.stat()
        result = read(path)
        after = path.stat()
        if _token(before) == _token(after):
            return result, after
    raise OSError(f"File changed while hashing: {path}")


def parse_python(source: bytes, path: Path) -> ast.Module:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            return ast.parse(source, filename=str(path))
    except (SyntaxError, ValueError) as error:
        raise ValueError(f"Cannot parse Python source file {path}: {error}") from error


def normalized_ast(source: bytes, path: Path) -> str:
    """``ast.dump`` without positions (comments and formatting do not matter)."""

    return ast.dump(parse_python(source, path), include_attributes=False)


@dataclass(frozen=True, slots=True)
class CachedStat:
    """One ``stat_cache`` row: hashes valid while ``(inode, size, mtime_ns)`` match."""

    inode: int
    size: int
    mtime_ns: int
    content_hash: str
    ast_hash: str | None = None

    def matches(self, st: os.stat_result) -> bool:
        return (self.inode, self.size, self.mtime_ns) == _token(st)


class StatCache:
    """In-memory view of the ``stat_cache`` table with pending writes.

    The engine loads it from ``Repo.load_stat_cache()`` and periodically
    persists ``drain()`` via ``Repo.put_stat_cache()``.
    """

    def __init__(self, rows: Mapping[str, CachedStat] | None = None) -> None:
        self._rows: dict[str, CachedStat] = dict(rows or {})
        self._dirty: dict[str, CachedStat] = {}

    def lookup(self, path: str, st: os.stat_result) -> CachedStat | None:
        row = self._rows.get(path)
        return row if row is not None and row.matches(st) else None

    def store(self, path: str, row: CachedStat) -> None:
        self._rows[path] = row
        self._dirty[path] = row

    def drain(self) -> dict[str, CachedStat]:
        dirty, self._dirty = self._dirty, {}
        return dirty


@dataclass(frozen=True)
class InputDigest:
    """Fingerprint of one named input: ``digest`` over the material (no absolute paths)."""

    digest: str
    files: int
    bytes: int
    material: list[Any]
    roots: list[str]


@dataclass(frozen=True)
class ArtifactFingerprint:
    """Managed output of one unit: ``manifest`` rows are ``[relpath, size, mtime_ns, sha]``."""

    fingerprint: str
    manifest: list[list[Any]]
    files: int
    bytes: int


@lru_cache(maxsize=256)
def _glob_regex(pattern: str) -> re.Pattern[str]:
    """Translate a POSIX glob with ``**`` into a regex over ``/``-separated relpaths."""

    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        elif pattern[i] == "[":
            end = pattern.find("]", i + 1)
            if end == -1:
                out.append(re.escape("["))
                i += 1
            else:
                body = pattern[i + 1 : end]
                out.append("[" + ("^" + body[1:] if body.startswith("!") else body) + "]")
                i = end + 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def glob_match(relpath: str, pattern: str) -> bool:
    return _glob_regex(pattern).match(relpath) is not None


def _resolve_root(root: Path) -> Path:
    """Resolve a declared root (which may be or sit behind a symlink) to a file or directory."""

    try:
        resolved = root.expanduser().resolve(strict=True)
    except FileNotFoundError:
        raise FileNotFoundError(f"Declared path does not exist: {root}") from None
    if not resolved.is_file() and not resolved.is_dir():
        raise ValueError(f"Declared path is neither a file nor a directory: {root}")
    return resolved


def walk_files(root: Path) -> list[tuple[str, Path]]:
    """Sorted ``(relpath, path)`` of regular files under ``root`` (a file yields itself).

    ``root`` itself may be or sit behind a symlink; entries below it must not
    be symlinks or special files.
    """

    resolved = _resolve_root(root)
    if resolved.is_file():
        return [(resolved.name, resolved)]
    found: list[tuple[str, Path]] = []
    pending: list[tuple[str, str]] = [("", str(resolved))]
    while pending:
        prefix, directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                rel = f"{prefix}{entry.name}"
                if entry.is_symlink():
                    raise ValueError(
                        f"Symlinks are not allowed inside declared trees: {entry.path}"
                    )
                if entry.is_dir(follow_symlinks=False):
                    pending.append((f"{rel}/", entry.path))
                elif entry.is_file(follow_symlinks=False):
                    found.append((rel, Path(entry.path)))
                else:
                    raise ValueError(f"Unsupported file type inside declared tree: {entry.path}")
    found.sort(key=lambda item: item[0])
    return found


def _segment_could_descend(dir_parts: list[str], pattern_parts: list[str]) -> bool:
    """Whether some path strictly below the directory ``dir_parts`` can match the pattern.

    Pattern segments are compared with directory segments one by one; a
    segment containing ``**`` can span any number of segments, so everything
    below it is reachable.
    """

    for index, part in enumerate(dir_parts):
        if index >= len(pattern_parts):
            return False
        segment = pattern_parts[index]
        if "**" in segment:
            return True
        if not glob_match(part, segment):
            return False
    return len(pattern_parts) > len(dir_parts)


def _excludes_subtree(rel: str, exclude: tuple[str, ...]) -> bool:
    """Whether ``exclude`` removes every path below directory ``rel`` (``**`` or ``<dir>/**``)."""

    return any(
        pattern == "**" or (pattern.endswith("/**") and glob_match(rel, pattern[:-3]))
        for pattern in exclude
    )


def walk_glob(
    root: Path, include: tuple[str, ...], exclude: tuple[str, ...]
) -> list[tuple[str, Path]]:
    """Sorted ``(relpath, path)`` of files under ``root`` matching ``include`` minus ``exclude``.

    Only the parts of the tree an include pattern can reach are walked.
    ``root`` itself may be or sit behind a symlink. Below it, a symlink or
    special file is an error only when the glob would select it: a matched,
    non-excluded entry, or a symlinked directory below which an include
    pattern could match something that is not excluded. Everything else,
    including excluded paths, is ignored.
    """

    resolved = _resolve_root(root)

    def selected(rel: str) -> bool:
        return any(glob_match(rel, p) for p in include) and not any(
            glob_match(rel, p) for p in exclude
        )

    if resolved.is_file():
        return [(resolved.name, resolved)] if selected(resolved.name) else []
    split_include = [pattern.split("/") for pattern in include]

    def reaches_below(rel: str) -> bool:
        if _excludes_subtree(rel, exclude):
            return False
        parts = rel.split("/")
        return any(_segment_could_descend(parts, pattern) for pattern in split_include)

    found: list[tuple[str, Path]] = []
    pending: list[tuple[str, str]] = [("", str(resolved))]
    while pending:
        prefix, directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                rel = f"{prefix}{entry.name}"
                if entry.is_symlink():
                    if selected(rel) or (
                        entry.is_dir()
                        and not any(glob_match(rel, p) for p in exclude)
                        and reaches_below(rel)
                    ):
                        raise ValueError(
                            f"Symlinks are not allowed inside declared trees: {entry.path}"
                        )
                elif entry.is_dir(follow_symlinks=False):
                    if reaches_below(rel):
                        pending.append((f"{rel}/", entry.path))
                elif selected(rel):
                    if not entry.is_file(follow_symlinks=False):
                        raise ValueError(
                            f"Unsupported file type inside declared tree: {entry.path}"
                        )
                    found.append((rel, Path(entry.path)))
    found.sort(key=lambda item: item[0])
    return found


class FingerprintSession:
    """Filesystem observations shared within one command (probe or run).

    Content hashes go through the optional :class:`StatCache` so unchanged
    files are not re-read across commands; ``rehash=True`` ignores it.
    """

    def __init__(self, cache: StatCache | None = None, *, rehash: bool = False) -> None:
        self.cache = cache if cache is not None else StatCache()
        self.rehash = rehash
        self._trees: dict[Path, list[tuple[str, Path]]] = {}
        self._globs: dict[
            tuple[Path, tuple[str, ...], tuple[str, ...]], list[tuple[str, Path]]
        ] = {}
        self._hashes: dict[Path, tuple[str, os.stat_result]] = {}

    def walk(self, root: Path) -> list[tuple[str, Path]]:
        key = root.expanduser().absolute()
        tree = self._trees.get(key)
        if tree is None:
            tree = walk_files(key)
            self._trees[key] = tree
        return tree

    def walk_glob(self, spec: Glob) -> list[tuple[str, Path]]:
        key = (spec.root.expanduser().absolute(), spec.include, spec.exclude)
        files = self._globs.get(key)
        if files is None:
            files = walk_glob(key[0], spec.include, spec.exclude)
            self._globs[key] = files
        return files

    def file_hash(self, path: Path) -> tuple[str, os.stat_result]:
        """Content sha256 and stat of ``path`` (a resolved file path)."""

        memo = self._hashes.get(path)
        if memo is not None:
            return memo
        st = path.stat()
        cached = None if self.rehash else self.cache.lookup(str(path), st)
        if cached is not None:
            result = (cached.content_hash, st)
        else:
            digest, st = _stable(path, _sha256_file)
            previous = self.cache.lookup(str(path), st)
            self.cache.store(
                str(path),
                CachedStat(*_token(st), digest, previous.ast_hash if previous else None),
            )
            result = (digest, st)
        self._hashes[path] = result
        return result

    def ast_hash(self, path: Path) -> str:
        """Hash of the normalized AST of a Python file, through the stat cache."""

        resolved = path.expanduser().resolve(strict=True)
        st = resolved.stat()
        cached = None if self.rehash else self.cache.lookup(str(resolved), st)
        if cached is not None and cached.ast_hash is not None:
            return cached.ast_hash
        source, st = _stable(resolved, Path.read_bytes)
        tree_hash = hashlib.sha256(normalized_ast(source, resolved).encode("utf-8")).hexdigest()
        content = hashlib.sha256(source).hexdigest()
        self.cache.store(str(resolved), CachedStat(*_token(st), content, tree_hash))
        return tree_hash

    def _set_files(self, spec: PathSet | Glob) -> Iterator[tuple[str, list[tuple[str, Path]]]]:
        if isinstance(spec, PathSet):
            for path in spec.paths:
                yield str(path.expanduser().absolute()), self.walk(path)
            return
        files = self.walk_glob(spec)
        if not files and not spec.allow_empty:
            raise FileNotFoundError(
                f"Dependencies.glob matched no files under {spec.root} "
                f"(include={list(spec.include)}, exclude={list(spec.exclude)})"
            )
        yield str(spec.root.expanduser().absolute()), files

    def input_digest(self, spec: InputSpec) -> InputDigest:
        normalized = as_input_set(spec)
        material: list[Any] = []
        roots: list[str] = []
        count = 0
        total = 0
        for root, files in self._set_files(normalized):
            rows: list[list[Any]] = []
            for rel, path in files:
                if normalized.mode == "stat":
                    st = path.stat()
                    rows.append([rel, st.st_size, st.st_mtime_ns])
                else:
                    digest, st = self.file_hash(path)
                    rows.append([rel, digest])
                total += st.st_size
            count += len(rows)
            material.append([normalized.mode, rows])
            roots.append(root)
        return InputDigest(json_hash(material), count, total, material, roots)

    def inputs(self, specs: Mapping[str, InputSpec]) -> dict[str, InputDigest]:
        return {name: self.input_digest(specs[name]) for name in sorted(specs)}


def artifact_fingerprint(unit_dir: Path) -> ArtifactFingerprint:
    """Hash every managed file of a freshly written unit directory (never cached)."""

    manifest: list[list[Any]] = []
    total = 0
    for rel, path in walk_files(unit_dir):
        digest, st = _stable(path, _sha256_file)
        manifest.append([rel, st.st_size, st.st_mtime_ns, digest])
        total += st.st_size
    fingerprint = json_hash([[rel, digest] for rel, _, _, digest in manifest])
    return ArtifactFingerprint(fingerprint, manifest, len(manifest), total)


def is_real_dir(path: str) -> bool:
    """Whether ``path`` exists as a directory that is not a symlink."""

    try:
        return stat_module.S_ISDIR(os.lstat(path).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False


def verify_manifest(unit_dir: Path, manifest: list[list[Any]], *, rehash: bool = False) -> bool:
    """Whether the unit directory is a real directory and every manifest file still
    exists with the same size and mtime (or hash). The directory check keeps a unit
    with an empty manifest from verifying after its directory was removed."""

    if not is_real_dir(str(unit_dir)):
        return False
    for rel, size, mtime_ns, digest in manifest:
        path = unit_dir / rel
        try:
            st = path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return False
        if not stat_module.S_ISREG(st.st_mode):
            return False
        if rehash:
            if _sha256_file(path) != digest:
                return False
        elif (st.st_size, st.st_mtime_ns) != (size, mtime_ns):
            return False
    return True
