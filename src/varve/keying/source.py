"""Callable AST and residual-file Python source observation."""

from __future__ import annotations

import ast
import copy
import inspect
import warnings
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from varve.dependencies import Dependencies, merge_dependencies
from varve.keying.fingerprint import assert_no_symlink_path, json_sha256
from varve.models import SourceFingerprint, SourceManifestEntry, SourceObservation


def _definition_file(value: Any, description: str) -> Path:
    source = inspect.getsourcefile(value)
    if source is None:
        raise ValueError(f"Cannot locate Python source file for {description}")
    source_path = Path(source)
    assert_no_symlink_path(source_path, description="source paths")
    path = source_path.resolve()
    if not path.is_file():
        raise ValueError(f"Python source file does not exist for {description}: {path}")
    return path


def _resolve_declared_root(path: Path, declaration_base: Path) -> Path:
    resolved = (path if path.is_absolute() else declaration_base / path).expanduser()
    assert_no_symlink_path(resolved, description="source paths")
    return resolved.absolute()


def _display_path(path: Path, declaration_base: Path) -> str:
    try:
        return path.relative_to(declaration_base).as_posix()
    except ValueError:
        return path.as_posix()


def _declared_roots(
    depends: Dependencies,
    declaration_base: Path,
) -> tuple[dict[Path, str], dict[Path, str]]:
    rerun: dict[Path, str] = {}
    review: dict[Path, str] = {}
    for path in depends.sources:
        root = _resolve_declared_root(path, declaration_base)
        display = _display_path(root, declaration_base)
        rerun.setdefault(root, f"declared:{display}")
    for path in depends.review_sources:
        root = _resolve_declared_root(path, declaration_base)
        display = _display_path(root, declaration_base)
        review.setdefault(root, f"review:{display}")
    conflicts = sorted(set(rerun) & set(review), key=str)
    if conflicts:
        details = ", ".join(_display_path(path, declaration_base) for path in conflicts)
        raise ValueError(
            "Dependencies.sources and review_sources declare the same root: " + details
        )
    return rerun, review


def _collect_python_files(root: Path) -> list[tuple[str, Path]]:
    assert_no_symlink_path(root, description="source paths")
    if not root.exists():
        raise FileNotFoundError(f"Declared source path does not exist: {root}")
    if root.is_file():
        if root.suffix != ".py":
            raise ValueError(f"Declared source file must end in .py: {root}")
        return [(root.name, root)]
    if not root.is_dir():
        raise ValueError(f"Unsupported source path: {root}")
    members = []
    for member in sorted(root.rglob("*")):
        if member.is_symlink():
            raise ValueError(f"Symlinks are not supported in source paths: {member}")
        if member.is_file() and member.suffix == ".py":
            members.append((member.relative_to(root).as_posix(), member))
    return members


def _read_module_ast(path: Path) -> tuple[ast.AST, Any]:
    try:
        source = path.read_bytes()
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(source, filename=str(path))
        after = path.stat()
    except (OSError, SyntaxError, UnicodeError) as error:
        raise ValueError(f"Cannot parse Python source file: {path}: {error}") from error
    return tree, after


def _source_entry_from_tree(
    label: str,
    path: Path,
    tree: ast.AST,
    after: Any,
) -> SourceManifestEntry:
    return SourceManifestEntry(
        path=label,
        cache_path=str(path.resolve()),
        digest=json_sha256(ast.dump(tree, annotate_fields=True, include_attributes=False)),
        inode=after.st_ino,
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
    )


def _matching_cached_entry(
    label: str,
    path: Path,
    cached: SourceManifestEntry | None,
    *,
    force_rehash: bool,
) -> SourceManifestEntry | None:
    if cached is None or force_rehash:
        return None
    try:
        stat = path.stat()
    except OSError:
        return None
    return (
        cached
        if cached.path == label
        and cached.cache_path == str(path.resolve())
        and cached.inode == stat.st_ino
        and cached.size == stat.st_size
        and cached.mtime_ns == stat.st_mtime_ns
        and cached.algorithm == "ast-sha256"
        else None
    )


def _source_entry(
    label: str,
    path: Path,
    cached: SourceManifestEntry | None,
    *,
    force_rehash: bool,
    retries: int = 2,
) -> SourceManifestEntry:
    hit = _matching_cached_entry(label, path, cached, force_rehash=force_rehash)
    if hit is not None:
        return hit
    for attempt in range(retries + 1):
        try:
            before = path.stat()
            tree, after = _read_module_ast(path)
        except OSError as error:
            raise ValueError(f"Cannot parse Python source file: {path}: {error}") from error
        before_token = (before.st_ino, before.st_size, before.st_mtime_ns)
        after_token = (after.st_ino, after.st_size, after.st_mtime_ns)
        if before_token == after_token:
            return _source_entry_from_tree(label, path, tree, after)
        if attempt == retries:
            raise ValueError(f"Python source file changed while fingerprinting: {path}")
    raise AssertionError("unreachable")


def _fingerprint_entries(entries: Iterable[SourceManifestEntry]) -> SourceFingerprint:
    files = sorted(entries, key=lambda item: item.path)
    return SourceFingerprint(
        fingerprint=json_sha256([{"path": entry.path, "digest": entry.digest} for entry in files]),
        files=list(files),
    )


def _locate_callable_node(tree: ast.AST, func: Any, description: str) -> ast.AST:
    name = getattr(func, "__name__", "") or ""
    code = getattr(func, "__code__", None)
    if code is None:
        raise ValueError(f"Cannot locate source line for callable {func!r}")
    first = int(code.co_firstlineno)
    async_expected = inspect.iscoroutinefunction(func) or inspect.isasyncgenfunction(func)
    matches: list[ast.AST] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != name:
            continue
        if isinstance(node, ast.AsyncFunctionDef) != async_expected:
            continue
        start = min(
            (decorator.lineno for decorator in node.decorator_list),
            default=node.lineno,
        )
        end = getattr(node, "end_lineno", None) or node.lineno
        if start <= first <= end:
            matches.append(node)
    if len(matches) != 1:
        raise ValueError(
            f"Cannot uniquely locate Stage callable AST for {description}: "
            f"found {len(matches)} candidates for {name!r} at line {first}"
        )
    return matches[0]


def _callable_entry_from_tree(
    label: str,
    path: Path,
    func: Any,
    description: str,
    tree: ast.AST,
    stat: Any,
) -> SourceManifestEntry:
    node = _locate_callable_node(tree, func, description)
    payload = {
        "identity": {
            "module": getattr(func, "__module__", "") or "",
            "qualname": getattr(func, "__qualname__", "") or getattr(func, "__name__", "") or "",
        },
        "node": ast.dump(node, annotate_fields=True, include_attributes=False),
    }
    return SourceManifestEntry(
        path=label,
        cache_path=str(path.resolve()),
        digest=json_sha256(payload),
        inode=stat.st_ino,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def _remove_nodes(tree: ast.AST, nodes: set[ast.AST]) -> ast.AST:
    """Remove callable nodes from ``tree`` in place. ``nodes`` must come from ``tree``."""

    class _Strip(ast.NodeTransformer):
        def visit(self, node: ast.AST) -> Any:
            return None if node in nodes else super().visit(node)

    return _Strip().visit(tree)


def _collect_stage_callables(
    pipeline_type: type[Any],
) -> list[tuple[str, Any, Path]]:
    stages = pipeline_type.stages()
    collected: list[tuple[str, Any, Path]] = []
    for name, spec in stages.items():
        path = _definition_file(spec.func, f"stage {name}")
        collected.append((name, spec.func, path))
    return collected


@dataclass
class SourceFingerprintSession:
    _cache: dict[tuple[Any, ...], SourceObservation] = field(default_factory=dict)
    force_rehash: bool = False
    _module_cache: dict[Path, tuple[Any, Any, str]] = field(default_factory=dict)
    _pipeline_callables: dict[type[Any], list[tuple[str, Any, Path]]] = field(default_factory=dict)

    def observe(
        self,
        pipeline_type: type[Any],
        stage_spec: Any,
        *,
        cached: tuple[SourceObservation | SourceFingerprint, ...] = (),
    ) -> SourceObservation:
        depends = merge_dependencies(pipeline_type.depends, stage_spec.depends)
        pipeline_file = _definition_file(pipeline_type, f"pipeline {pipeline_type.__name__}")
        stage_file = _definition_file(stage_spec.func, f"stage {stage_spec.name}")
        declaration_base = pipeline_file.parent
        rerun_roots, review_roots = _declared_roots(depends, declaration_base)
        key = (
            pipeline_type,
            id(stage_spec.func),
            tuple(str(path) for path in sorted(rerun_roots, key=str)),
            tuple(str(path) for path in sorted(review_roots, key=str)),
        )
        observed = self._cache.get(key)
        if observed is not None:
            return observed

        cached_entries: dict[str, SourceManifestEntry] = {}
        for item in cached:
            if isinstance(item, SourceObservation):
                for entry in (*item.rerun.files, *item.review.files):
                    cached_entries.setdefault(entry.path, entry)
            else:
                for entry in item.files:
                    cached_entries.setdefault(entry.path, entry)

        stage_callables = self._pipeline_callables.get(pipeline_type)
        if stage_callables is None:
            stage_callables = _collect_stage_callables(pipeline_type)
            self._pipeline_callables[pipeline_type] = stage_callables

        residual_files = [pipeline_file]
        if stage_file != pipeline_file:
            residual_files.append(stage_file)
        residual_labels = {
            path: f"residual:{_display_path(path, declaration_base)}" for path in residual_files
        }
        residual_cache_hits = all(
            _matching_cached_entry(
                label,
                path,
                cached_entries.get(label),
                force_rehash=self.force_rehash,
            )
            is not None
            for path, label in residual_labels.items()
        )

        rerun_entries: dict[str, SourceManifestEntry] = {}
        review_entries: dict[str, SourceManifestEntry] = {}
        rerun_file_paths: set[str] = set()

        callable_label = f"callable:{stage_spec.base_name or stage_spec.name}"
        callable_entry = _matching_cached_entry(
            callable_label,
            stage_file,
            cached_entries.get(callable_label),
            force_rehash=self.force_rehash,
        )
        if callable_entry is None:
            tree, stat = self._module_tree(stage_file)
            callable_entry = _callable_entry_from_tree(
                callable_label,
                stage_file,
                stage_spec.func,
                f"stage {stage_spec.name}",
                tree,
                stat,
            )
        rerun_entries[callable_label] = callable_entry

        if residual_cache_hits:
            for residual_path, label in residual_labels.items():
                review_entries[label] = cached_entries[label]
        else:
            callables_by_file: dict[Path, list[tuple[str, Any]]] = {}
            for name, func, path in stage_callables:
                if path in residual_labels:
                    callables_by_file.setdefault(path, []).append((name, func))
            for residual_path, label in residual_labels.items():
                tree, after = self._module_tree(residual_path)
                working = copy.deepcopy(tree)
                nodes = {
                    _locate_callable_node(working, func, f"stage {name}")
                    for name, func in callables_by_file.get(residual_path, ())
                }
                residual = _remove_nodes(working, nodes)
                review_entries[label] = _source_entry_from_tree(
                    label, residual_path, residual, after
                )

        for root, label in sorted(rerun_roots.items(), key=lambda item: item[1]):
            for relative, member in _collect_python_files(root):
                member_label = f"{label}/{relative}"
                entry = _source_entry(
                    member_label,
                    member,
                    cached_entries.get(member_label),
                    force_rehash=self.force_rehash,
                )
                rerun_entries[member_label] = entry
                rerun_file_paths.add(str(member.resolve()))

        for root, label in sorted(review_roots.items(), key=lambda item: item[1]):
            for relative, member in _collect_python_files(root):
                resolved = str(member.resolve())
                if resolved in rerun_file_paths:
                    continue
                member_label = f"{label}/{relative}"
                entry = _source_entry(
                    member_label,
                    member,
                    cached_entries.get(member_label),
                    force_rehash=self.force_rehash,
                )
                review_entries[member_label] = entry

        result = SourceObservation(
            rerun=_fingerprint_entries(rerun_entries.values()),
            review=_fingerprint_entries(review_entries.values()),
        )
        self._cache[key] = result
        return result

    def _module_tree(self, path: Path) -> tuple[ast.AST, Any]:
        cache_path = str(path.resolve())
        for attempt in range(3):
            before = path.stat()
            cached = self._module_cache.get(path)
            if (
                cached is not None
                and not self.force_rehash
                and cached[2] == cache_path
                and cached[1].st_ino == before.st_ino
                and cached[1].st_size == before.st_size
                and cached[1].st_mtime_ns == before.st_mtime_ns
            ):
                return cached[0], cached[1]
            tree, after = _read_module_ast(path)
            before_token = (before.st_ino, before.st_size, before.st_mtime_ns)
            after_token = (after.st_ino, after.st_size, after.st_mtime_ns)
            if before_token == after_token:
                self._module_cache[path] = (tree, after, cache_path)
                return tree, after
            if attempt == 2:
                raise ValueError(f"Python source file changed while fingerprinting: {path}")
        raise AssertionError("unreachable")
