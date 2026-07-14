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
from varve.keying.fingerprint import _stat_token, assert_no_symlink_path, json_sha256
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
    for paths, prefix, target in (
        (depends.sources, "declared", rerun),
        (depends.review_sources, "review", review),
    ):
        for path in paths:
            root = (path if path.is_absolute() else declaration_base / path).expanduser()
            assert_no_symlink_path(root, description="source paths")
            root = root.absolute()
            target.setdefault(root, f"{prefix}:{_display_path(root, declaration_base)}")
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


def _stable_module_ast(path: Path, *, before: Any | None = None) -> tuple[ast.AST, Any]:
    for _attempt in range(3):
        try:
            start = before if before is not None else path.stat()
            before = None
            source = path.read_bytes()
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(source, filename=str(path))
            after = path.stat()
        except (OSError, SyntaxError, UnicodeError) as error:
            raise ValueError(f"Cannot parse Python source file: {path}: {error}") from error
        if _stat_token(start) == _stat_token(after):
            return tree, after
    raise ValueError(f"Python source file changed while fingerprinting: {path}")


def _manifest_entry(label: str, path: Path, digest: str, stat: Any) -> SourceManifestEntry:
    return SourceManifestEntry(
        path=label,
        cache_path=str(path.resolve()),
        digest=digest,
        inode=stat.st_ino,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )


def _source_entry_from_tree(
    label: str, path: Path, tree: ast.AST, stat: Any
) -> SourceManifestEntry:
    digest = json_sha256(ast.dump(tree, annotate_fields=True, include_attributes=False))
    return _manifest_entry(label, path, digest, stat)


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
        and (cached.inode, cached.size, cached.mtime_ns) == _stat_token(stat)
        and cached.algorithm == "ast-sha256"
        else None
    )


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
    matches = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == name
        and isinstance(node, ast.AsyncFunctionDef) == async_expected
        and min((item.lineno for item in node.decorator_list), default=node.lineno)
        <= first
        <= (getattr(node, "end_lineno", None) or node.lineno)
    ]
    if len(matches) != 1:
        raise ValueError(
            f"Cannot uniquely locate Stage callable AST for {description}: "
            f"found {len(matches)} candidates for {name!r} at line {first}"
        )
    return matches[0]


def _remove_nodes(tree: ast.AST, nodes: set[ast.AST]) -> ast.AST:
    """Remove callable nodes from ``tree`` in place. ``nodes`` must come from ``tree``."""

    class _Strip(ast.NodeTransformer):
        def visit(self, node: ast.AST) -> Any:
            return None if node in nodes else super().visit(node)

    return _Strip().visit(tree)


def _declared_entries(
    roots: dict[Path, str],
    cached: dict[str, SourceManifestEntry],
    *,
    force_rehash: bool,
    excluded_paths: set[str] | None = None,
) -> tuple[dict[str, SourceManifestEntry], set[str]]:
    entries: dict[str, SourceManifestEntry] = {}
    paths: set[str] = set()
    for root, label in sorted(roots.items(), key=lambda item: item[1]):
        for relative, member in _collect_python_files(root):
            resolved = str(member.resolve())
            if excluded_paths is not None and resolved in excluded_paths:
                continue
            member_label = f"{label}/{relative}"
            entry = _matching_cached_entry(
                member_label,
                member,
                cached.get(member_label),
                force_rehash=force_rehash,
            )
            if entry is None:
                tree, after = _stable_module_ast(member)
                entry = _source_entry_from_tree(member_label, member, tree, after)
            entries[member_label] = entry
            paths.add(resolved)
    return entries, paths


@dataclass
class SourceFingerprintSession:
    _cache: dict[tuple[Any, ...], SourceObservation] = field(default_factory=dict)
    force_rehash: bool = False
    _module_cache: dict[Path, tuple[Any, Any]] = field(default_factory=dict)
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
            fingerprints = (
                (item.rerun, item.review) if isinstance(item, SourceObservation) else (item,)
            )
            for fingerprint in fingerprints:
                for entry in fingerprint.files:
                    cached_entries.setdefault(entry.path, entry)

        stage_callables = self._pipeline_callables.get(pipeline_type)
        if stage_callables is None:
            stage_callables = [
                (name, spec.func, _definition_file(spec.func, f"stage {name}"))
                for name, spec in pipeline_type.stages().items()
            ]
            self._pipeline_callables[pipeline_type] = stage_callables

        residual_labels = {
            path: f"residual:{_display_path(path, declaration_base)}"
            for path in dict.fromkeys((pipeline_file, stage_file))
        }
        rerun_entries: dict[str, SourceManifestEntry] = {}
        review_entries: dict[str, SourceManifestEntry] = {}

        callable_label = f"callable:{stage_spec.base_name or stage_spec.name}"
        callable_entry = _matching_cached_entry(
            callable_label,
            stage_file,
            cached_entries.get(callable_label),
            force_rehash=self.force_rehash,
        )
        if callable_entry is None:
            tree, stat = self._module_tree(stage_file)
            func = stage_spec.func
            identity = {
                "module": getattr(func, "__module__", "") or "",
                "qualname": getattr(func, "__qualname__", "")
                or getattr(func, "__name__", "")
                or "",
            }
            node = _locate_callable_node(tree, func, f"stage {stage_spec.name}")
            digest = json_sha256(
                {
                    "identity": identity,
                    "node": ast.dump(node, annotate_fields=True, include_attributes=False),
                }
            )
            callable_entry = _manifest_entry(callable_label, stage_file, digest, stat)
        rerun_entries[callable_label] = callable_entry

        callables_by_file = {
            path: [(name, func) for name, func, source in stage_callables if source == path]
            for path in residual_labels
        }
        for residual_path, label in residual_labels.items():
            cached_entry = _matching_cached_entry(
                label,
                residual_path,
                cached_entries.get(label),
                force_rehash=self.force_rehash,
            )
            if cached_entry is not None:
                review_entries[label] = cached_entry
                continue
            tree, after = self._module_tree(residual_path)
            working = copy.deepcopy(tree)
            nodes = {
                _locate_callable_node(working, func, f"stage {name}")
                for name, func in callables_by_file.get(residual_path, ())
            }

            review_entries[label] = _source_entry_from_tree(
                label, residual_path, _remove_nodes(working, nodes), after
            )

        declared_rerun, rerun_file_paths = _declared_entries(
            rerun_roots,
            cached_entries,
            force_rehash=self.force_rehash,
        )
        declared_review, _ = _declared_entries(
            review_roots,
            cached_entries,
            force_rehash=self.force_rehash,
            excluded_paths=rerun_file_paths,
        )
        rerun_entries.update(declared_rerun)
        review_entries.update(declared_review)

        result = SourceObservation(
            rerun=_fingerprint_entries(rerun_entries.values()),
            review=_fingerprint_entries(review_entries.values()),
        )
        self._cache[key] = result
        return result

    def _module_tree(self, path: Path) -> tuple[ast.AST, Any]:
        before = path.stat()
        cached = self._module_cache.get(path)
        if (
            cached is not None
            and not self.force_rehash
            and _stat_token(cached[1]) == _stat_token(before)
        ):
            return cached[0], cached[1]
        tree, after = _stable_module_ast(path, before=before)
        self._module_cache[path] = (tree, after)
        return tree, after
