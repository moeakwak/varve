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
from collections.abc import Callable, Mapping
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


# One shared encoder: ``json.dumps`` with non-default options builds a new one per call.
CANONICAL_ENCODER = json.JSONEncoder(
    sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
)
"""Encoder of canonical key JSON (sorted keys, compact, UTF-8 text, no NaN)."""


def canonical_json(obj: Any) -> bytes:
    return CANONICAL_ENCODER.encode(_normalize_for_json(obj)).encode("utf-8")


def json_hash(obj: Any) -> str:
    """Hex sha256 of the canonical JSON of ``obj``."""

    return hashlib.sha256(canonical_json(obj)).hexdigest()


def _sha256_file(path: Path | str) -> str:
    with open(path, "rb") as handle:
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

    The engine passes ``Repo.load_stat_cache`` as ``loader``; the table is read
    on the first lookup, so commands that hash no file never load it. Pending
    writes are persisted with ``drain()`` via ``Repo.put_stat_cache()``.
    """

    def __init__(
        self,
        rows: Mapping[str, CachedStat] | None = None,
        *,
        loader: Callable[[], Mapping[str, CachedStat]] | None = None,
    ) -> None:
        self._rows: dict[str, CachedStat] = dict(rows or {})
        self._dirty: dict[str, CachedStat] = {}
        self._loader = loader

    def _load(self) -> None:
        loader, self._loader = self._loader, None
        if loader is not None:
            self._rows = {**loader(), **self._rows}

    def lookup(self, path: str, st: os.stat_result) -> CachedStat | None:
        if self._loader is not None:
            self._load()
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
    return [(rel, Path(path)) for rel, path in _scan_tree(str(resolved))]


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
) -> list[tuple[str, str]]:
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
        return [(resolved.name, str(resolved))] if selected(resolved.name) else []
    split_include = [pattern.split("/") for pattern in include]

    def reaches_below(rel: str) -> bool:
        if _excludes_subtree(rel, exclude):
            return False
        parts = rel.split("/")
        return any(_segment_could_descend(parts, pattern) for pattern in split_include)

    found: list[tuple[str, str]] = []
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
                    found.append((rel, entry.path))
    found.sort(key=lambda item: item[0])
    return found


def _scan_tree(directory: str) -> list[tuple[str, str]]:
    """Sorted ``(relpath, path)`` of regular files below a resolved directory (see ``walk_files``)."""

    found: list[tuple[str, str]] = []
    pending: list[tuple[str, str]] = [("", directory)]
    while pending:
        prefix, current = pending.pop()
        with os.scandir(current) as entries:
            for entry in entries:
                rel = f"{prefix}{entry.name}"
                if entry.is_symlink():
                    raise ValueError(
                        f"Symlinks are not allowed inside declared trees: {entry.path}"
                    )
                if entry.is_dir(follow_symlinks=False):
                    pending.append((f"{rel}/", entry.path))
                elif entry.is_file(follow_symlinks=False):
                    found.append((rel, entry.path))
                else:
                    raise ValueError(f"Unsupported file type inside declared tree: {entry.path}")
    found.sort(key=lambda item: item[0])
    return found


def _material_text(entry: list[Any]) -> str:
    """Canonical JSON of one plain material entry (``[mode, rows]``)."""

    return CANONICAL_ENCODER.encode(entry)


@dataclass(frozen=True, slots=True)
class _Chunk:
    """What one declared root contributes to an input: ``entry`` is ``[mode, rows]``."""

    root: str
    entry: list[Any]
    files: int
    bytes: int
    text: str
    """Canonical JSON of ``entry``; an input's digest hashes ``[text, ...]``."""


class FingerprintSession:
    """Filesystem observations shared within one command (probe or run).

    Content hashes go through the optional :class:`StatCache` so unchanged
    files are not re-read across commands; ``rehash=True`` ignores it. Walks,
    file hashes, per-root material and whole input digests are memoized for
    the command: files are assumed not to change while it evaluates keys.
    Paths are handled as strings in the hot loops.
    """

    def __init__(self, cache: StatCache | None = None, *, rehash: bool = False) -> None:
        self.cache = cache if cache is not None else StatCache()
        self.rehash = rehash
        self._roots: dict[str, tuple[str, os.stat_result | None]] = {}
        """Absolute declared root -> (resolved path, stat of a regular file root or None)."""
        self._trees: dict[str, list[tuple[str, str]]] = {}
        self._realdirs: dict[str, str] = {}
        self._hashes: dict[str, tuple[str, os.stat_result]] = {}
        self._chunks: dict[tuple[Any, ...], _Chunk] = {}
        self._digests: dict[PathSet | Glob, InputDigest] = {}

    # -- roots and walks ------------------------------------------------------

    def _realdir(self, directory: str) -> str:
        real = self._realdirs.get(directory)
        if real is None:
            real = self._realdirs[directory] = os.path.realpath(directory)
        return real

    def _root(self, absolute: str) -> tuple[str, os.stat_result | None]:
        """Resolve a declared root like ``_resolve_root`` (same result, same errors).

        A root whose last component is not a symlink is resolved through its
        (memoized) parent directory, so a file costs one ``lstat``.
        """

        found = self._roots.get(absolute)
        if found is not None:
            return found
        try:
            st: os.stat_result | None = os.lstat(absolute)
        except OSError:
            st = None
        result: tuple[str, os.stat_result | None] | None = None
        if st is not None and not stat_module.S_ISLNK(st.st_mode):
            head, name = os.path.split(absolute)
            if stat_module.S_ISREG(st.st_mode) and name not in ("", ".", ".."):
                result = (os.path.join(self._realdir(head), name), st)
            elif stat_module.S_ISDIR(st.st_mode):
                result = (self._realdir(absolute), None)
        if result is None:
            resolved = _resolve_root(Path(absolute))  # symlinks, special files, errors
            result = (str(resolved), resolved.stat() if resolved.is_file() else None)
        self._roots[absolute] = result
        return result

    def _tree(self, absolute: str) -> list[tuple[str, str]]:
        tree = self._trees.get(absolute)
        if tree is None:
            resolved, file_stat = self._root(absolute)
            if file_stat is not None:
                tree = [(os.path.basename(resolved), resolved)]
            else:
                tree = _scan_tree(resolved)
            self._trees[absolute] = tree
        return tree

    def walk(self, root: Path) -> list[tuple[str, Path]]:
        absolute = str(root.expanduser().absolute())
        return [(rel, Path(path)) for rel, path in self._tree(absolute)]

    # -- hashes ---------------------------------------------------------------

    def _file_hash(self, path: str, st: os.stat_result | None = None) -> tuple[str, os.stat_result]:
        memo = self._hashes.get(path)
        if memo is not None:
            return memo
        if st is None:
            st = os.stat(path)
        cached = None if self.rehash else self.cache.lookup(path, st)
        if cached is not None:
            result = (cached.content_hash, st)
        else:
            digest, st = _stable(Path(path), _sha256_file)
            previous = self.cache.lookup(path, st)
            self.cache.store(
                path, CachedStat(*_token(st), digest, previous.ast_hash if previous else None)
            )
            result = (digest, st)
        self._hashes[path] = result
        return result

    def file_hash(self, path: Path) -> tuple[str, os.stat_result]:
        """Content sha256 and stat of ``path`` (a resolved file path)."""

        return self._file_hash(str(path))

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

    # -- inputs ---------------------------------------------------------------

    def _chunk(
        self, root: str, files: list[tuple[str, str]], mode: str, file_stat: os.stat_result | None
    ) -> _Chunk:
        rows: list[list[Any]] = []
        total = 0
        if mode == "stat":
            for rel, path in files:
                st = file_stat if file_stat is not None else os.stat(path)
                rows.append([rel, st.st_size, st.st_mtime_ns])
                total += st.st_size
        else:
            for rel, path in files:
                digest, st = self._file_hash(path, file_stat)
                rows.append([rel, digest])
                total += st.st_size
        entry = [mode, rows]
        return _Chunk(root, entry, len(rows), total, _material_text(entry))

    def _path_chunk(self, path: Path, mode: str) -> _Chunk:
        key = (path, mode)
        chunk = self._chunks.get(key)
        if chunk is None:
            absolute = str(path.expanduser().absolute())
            files = self._tree(absolute)
            resolved, file_stat = self._root(absolute)
            chunk = self._chunk(absolute, files, mode, file_stat)
            self._chunks[key] = chunk
        return chunk

    def _glob_chunk(self, spec: Glob) -> _Chunk:
        key = (spec.root, spec.include, spec.exclude, spec.mode, "glob")
        chunk = self._chunks.get(key)
        if chunk is None:
            absolute = spec.root.expanduser().absolute()
            files = walk_glob(absolute, spec.include, spec.exclude)
            chunk = self._chunk(str(absolute), files, spec.mode, None)
            self._chunks[key] = chunk
        if not chunk.files and not spec.allow_empty:
            raise FileNotFoundError(
                f"Dependencies.glob matched no files under {spec.root} "
                f"(include={list(spec.include)}, exclude={list(spec.exclude)})"
            )
        return chunk

    def input_digest(self, spec: InputSpec) -> InputDigest:
        normalized = as_input_set(spec)
        digest = self._digests.get(normalized)
        if digest is not None:
            return digest
        if isinstance(normalized, PathSet):
            chunks = [self._path_chunk(path, normalized.mode) for path in normalized.paths]
        else:
            chunks = [self._glob_chunk(normalized)]
        # Equals json_hash(material): the canonical JSON of a list joins its items' JSON.
        text = "[" + ",".join(chunk.text for chunk in chunks) + "]"
        digest = InputDigest(
            hashlib.sha256(text.encode("utf-8")).hexdigest(),
            sum(chunk.files for chunk in chunks),
            sum(chunk.bytes for chunk in chunks),
            [chunk.entry for chunk in chunks],
            [chunk.root for chunk in chunks],
        )
        self._digests[normalized] = digest
        return digest

    def inputs(self, specs: Mapping[str, InputSpec]) -> dict[str, InputDigest]:
        return {name: self.input_digest(specs[name]) for name in sorted(specs)}


def _hash_open_file(path: str, retries: int = 2) -> tuple[str, os.stat_result]:
    """Content sha256 and ``fstat`` of a file whose stat token is unchanged across the read."""

    for _ in range(retries + 1):
        with open(path, "rb") as handle:
            before = os.fstat(handle.fileno())
            file_digest = getattr(hashlib, "file_digest", None)  # Python >= 3.11
            if file_digest is not None:
                digest = file_digest(handle, "sha256").hexdigest()
            else:
                hasher = hashlib.sha256()
                for chunk in iter(lambda: handle.read(_CHUNK), b""):
                    hasher.update(chunk)
                digest = hasher.hexdigest()
            after = os.fstat(handle.fileno())
        if _token(before) == _token(after):
            return digest, after
    raise OSError(f"File changed while hashing: {path}")


def artifact_fingerprint(
    unit_dir: Path, *, read_only: bool = False, file_mode: int = 0o444, dir_mode: int = 0o755
) -> ArtifactFingerprint:
    """Hash every managed file of a freshly written unit directory (never cached).

    Entries must be regular files and directories (no symlinks). With
    ``read_only``, each file is set to ``file_mode`` right after hashing and
    every directory to ``dir_mode`` (``chmod`` does not change ``mtime``).
    """

    manifest: list[list[Any]] = []
    total = 0
    root = os.fspath(unit_dir)
    pending: list[tuple[str, str]] = [("", root)]
    while pending:
        prefix, directory = pending.pop()
        if read_only:
            os.chmod(directory, dir_mode)
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
                    digest, st = _hash_open_file(entry.path)
                    if read_only:
                        os.chmod(entry.path, file_mode)
                    manifest.append([rel, st.st_size, st.st_mtime_ns, digest])
                    total += st.st_size
                else:
                    raise ValueError(f"Unsupported file type inside declared tree: {entry.path}")
    manifest.sort(key=lambda row: row[0])
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
