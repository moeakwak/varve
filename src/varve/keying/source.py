"""Stage source fingerprints: single-file static symbol closure plus declared sources.

Every top-level name binding of the stage's defining file (``def``, ``class``,
assignments, imports, and names bound inside top-level compound statements)
is a symbol. A stage's closure starts at its method (with decorators) and the
class header, follows same-class members reached through ``self.x`` /
``cls.x`` / ``ClassName.x`` / ``type(self).x`` / ``self.__class__.x``, and
follows referenced module symbols transitively. Every non-method class-body
statement of the stage's class (and of the pipeline class) other than the
``Config``/``Args`` binding -- ``defaults``, class attributes, ``Resource`` /
``Axis`` / ``Ref`` attributes, nested classes -- and every dunder method
(``__init__``, ...) is in every stage's closure, as are module-level
statements that mutate the class (``Demo.SCALE = 2``). Other methods enter
only when reached from the stage or from an included class-body statement. Statements that bind no name form a shared preamble
included in every stage. The classes bound to the pipeline's ``Config`` and
``Args`` are never included (field projection covers them), nor is the
``if __name__ == "__main__":`` block. The scheduling-only keywords of
``@stage(...)`` (``workers``, ``exclusive``, ``max_failures``, ``backend``)
are stripped before hashing, and so are those keys of a class-body
``defaults = {...}`` / ``dict(...)`` statement. Files using dynamic lookup (``globals()``,
``eval``, ``sys.modules[__name__]``, ...) and classes using
``getattr(self|cls|type(self)|self.__class__, <non-constant>)`` fall back to a
whole-file AST hash. Cross-file sources are only those declared in
``Dependencies.sources`` (whole-file normalized AST each).
"""

from __future__ import annotations

import ast
import copy
import hashlib
import inspect
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from varve.keying.fingerprint import FingerprintSession, json_hash, parse_python

PREAMBLE = "<preamble>"
WHOLE_FILE = "<file>"
_DYNAMIC_BUILTINS = frozenset({"globals", "locals", "exec", "eval"})
SCHEDULING_KEYWORDS = frozenset({"workers", "exclusive", "max_failures", "backend"})
"""``@stage`` keywords that only affect scheduling; they never enter ``source_fp``."""
_VARVE_STAGE_MODULES = frozenset({"varve", "varve.decorators"})


@dataclass(frozen=True)
class SourceFingerprint:
    """``fingerprint`` is the ``source_fp`` column; ``detail`` is ``source_detail_json``."""

    fingerprint: str
    detail: dict[str, Any]


def _stmt_hash(node: ast.AST) -> str:
    return hashlib.sha256(ast.dump(node, include_attributes=False).encode("utf-8")).hexdigest()


def _is_main_guard(stmt: ast.stmt) -> bool:
    test = stmt.test if isinstance(stmt, ast.If) else None
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def _base_name(node: ast.AST) -> str | None:
    while isinstance(node, ast.Attribute | ast.Subscript | ast.Starred):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


class _Binder(ast.NodeVisitor):
    """Names bound at module scope by one top-level statement."""

    def __init__(self) -> None:
        self.names: set[str] = set()
        self.star = False

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del):
            self.names.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del):
            name = _base_name(node)  # `X.attr = ...` mutates X
            if name:
                self.names.add(name)
        else:
            self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if isinstance(node.ctx, ast.Store | ast.Del):
            name = _base_name(node)
            if name:
                self.names.add(name)
            self.visit(node.slice)
        else:
            self.generic_visit(node)

    def _scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.names.add(node.name)  # do not descend: inner names are local

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scope(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        return None

    # Comprehension targets are local to the comprehension.
    def visit_ListComp(self, node: ast.ListComp) -> None:
        return None

    def visit_SetComp(self, node: ast.SetComp) -> None:
        return None

    def visit_DictComp(self, node: ast.DictComp) -> None:
        return None

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        return None

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.names.add(alias.asname or alias.name.split(".")[0])

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name == "*":
                self.star = True
            else:
                self.names.add(alias.asname or alias.name)


def _is_owner(node: ast.AST, class_name: str | None) -> bool:
    """``self``/``cls``/``ClassName``, ``type(self)``/``type(cls)``, or ``self.__class__``."""

    owners = {"self", "cls"} | ({class_name} if class_name else set())
    if isinstance(node, ast.Name):
        return node.id in owners
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "type"
        and len(node.args) == 1
        and isinstance(node.args[0], ast.Name)
        and node.args[0].id in ("self", "cls")
    ):
        return True
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "__class__"
        and isinstance(node.value, ast.Name)
        and node.value.id in ("self", "cls")
    )


def _references(node: ast.AST, class_name: str | None) -> tuple[set[str], set[str]]:
    """Module names loaded in ``node`` and class members reached via self/cls/ClassName."""

    names: set[str] = set()
    members: set[str] = set()
    for child in ast.walk(node):
        if isinstance(child, ast.Name):
            names.add(child.id)
        elif isinstance(child, ast.Attribute) and _is_owner(child.value, class_name):
            members.add(child.attr)
    return names, members


def _class_dynamic_lookup(node: ast.ClassDef) -> str | None:
    """``getattr(<owner>, <non-constant>)`` anywhere in a class body."""

    for child in ast.walk(node):
        if (
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "getattr"
            and len(child.args) >= 2
            and _is_owner(child.args[0], node.name)
            and not isinstance(child.args[1], ast.Constant)
        ):
            return f"getattr({ast.unparse(child.args[0])}, <non-constant>) in class {node.name}"
    return None


def _dynamic_lookup(tree: ast.AST) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name):
                if func.id in _DYNAMIC_BUILTINS:
                    return f"{func.id}()"
                if func.id == "vars" and not node.args:
                    return "vars()"
                if func.id == "import_module" and _is_dunder_name(node.args):
                    return "import_module(__name__)"
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "import_module"
                and _is_dunder_name(node.args)
            ):
                return "importlib.import_module(__name__)"
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "modules"
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "sys"
            and isinstance(node.slice, ast.Name)
            and node.slice.id == "__name__"
        ):
            return "sys.modules[__name__]"
    return None


def _is_dunder_name(args: Sequence[ast.expr]) -> bool:
    return bool(args) and isinstance(args[0], ast.Name) and args[0].id == "__name__"


def _stage_aliases(tree: ast.Module) -> tuple[frozenset[str], frozenset[str]]:
    """Top-level names bound to varve's ``stage`` and to the ``varve`` package."""

    stages: set[str] = set()
    packages: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom) and stmt.level == 0:
            if stmt.module in _VARVE_STAGE_MODULES:
                stages.update(a.asname or a.name for a in stmt.names if a.name == "stage")
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name == "varve":
                    packages.add(alias.asname or "varve")
    return frozenset(stages), frozenset(packages)


class _StripScheduling(ast.NodeTransformer):
    """Drops scheduling-only keywords from ``@stage(...)`` decorator calls."""

    def __init__(self, stages: frozenset[str], packages: frozenset[str]) -> None:
        self.stages = stages
        self.packages = packages

    def is_stage(self, func: ast.expr) -> bool:
        if isinstance(func, ast.Name):
            return func.id in self.stages
        return (
            isinstance(func, ast.Attribute)
            and func.attr == "stage"
            and isinstance(func.value, ast.Name)
            and func.value.id in self.packages
        )

    def _function(self, node: Any) -> Any:
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and self.is_stage(decorator.func):
                decorator.keywords = [
                    keyword
                    for keyword in decorator.keywords
                    if keyword.arg not in SCHEDULING_KEYWORDS
                ]
        self.generic_visit(node)
        return node

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        return self._function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        return self._function(node)


def _is_scheduling_key(key: ast.expr | None) -> bool:
    return isinstance(key, ast.Constant) and key.value in SCHEDULING_KEYWORDS


def _strip_defaults(stmt: ast.stmt) -> ast.stmt:
    """``stmt`` without scheduling-only entries when it binds ``defaults`` to a
    dict display or a ``dict(...)`` call; otherwise ``stmt`` itself."""

    if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1:
        target, value = stmt.targets[0], stmt.value
    elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        target, value = stmt.target, stmt.value
    else:
        return stmt
    if not (isinstance(target, ast.Name) and target.id == "defaults"):
        return stmt
    if isinstance(value, ast.Dict) and any(_is_scheduling_key(k) for k in value.keys):
        stmt = copy.deepcopy(stmt)
        assert isinstance(stmt, ast.Assign | ast.AnnAssign) and isinstance(stmt.value, ast.Dict)
        pairs = [
            (k, v)
            for k, v in zip(stmt.value.keys, stmt.value.values, strict=True)
            if not _is_scheduling_key(k)
        ]
        stmt.value.keys = [k for k, _ in pairs]
        stmt.value.values = [v for _, v in pairs]
        return stmt
    if (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id == "dict"
        and any(k.arg in SCHEDULING_KEYWORDS for k in value.keywords)
    ):
        stmt = copy.deepcopy(stmt)
        assert isinstance(stmt, ast.Assign | ast.AnnAssign) and isinstance(stmt.value, ast.Call)
        stmt.value.keywords = [k for k in stmt.value.keywords if k.arg not in SCHEDULING_KEYWORDS]
        return stmt
    return stmt


@dataclass
class _Module:
    """Parsed symbol table of one file."""

    tree: ast.Module
    symbols: dict[str, list[ast.stmt]]
    preamble: list[ast.stmt]
    fallback_reason: str | None
    hashes: dict[int, str] = field(default_factory=dict)
    stage_names: frozenset[str] = frozenset()
    varve_packages: frozenset[str] = frozenset()
    stripped: dict[int, ast.stmt] = field(default_factory=dict)

    def stage_decorated(self, node: ast.AST) -> bool:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return False
        strip = _StripScheduling(self.stage_names, self.varve_packages)
        return any(
            strip.is_stage(d.func if isinstance(d, ast.Call) else d) for d in node.decorator_list
        )

    def _hashable(self, node: ast.AST) -> ast.AST:
        """``node`` with scheduling-only ``@stage`` keywords removed (a copy if needed)."""

        strip = _StripScheduling(self.stage_names, self.varve_packages)
        for child in ast.walk(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef) and any(
                isinstance(d, ast.Call)
                and strip.is_stage(d.func)
                and any(k.arg in SCHEDULING_KEYWORDS for k in d.keywords)
                for d in child.decorator_list
            ):
                return strip.visit(copy.deepcopy(node))
        return node

    def class_defaults(self, stmt: ast.stmt) -> ast.stmt:
        """A class-body ``defaults = {...}``/``dict(...)`` statement without its
        scheduling-only keys (a cached copy if any are present)."""

        stripped = self.stripped.get(id(stmt))
        if stripped is None:
            stripped = self.stripped[id(stmt)] = _strip_defaults(stmt)
        return stripped

    @classmethod
    def parse(cls, path: Path, source: bytes) -> _Module:
        tree = parse_python(source, path)
        symbols: dict[str, list[ast.stmt]] = {}
        preamble: list[ast.stmt] = []
        reason = _dynamic_lookup(tree)
        for stmt in tree.body:
            if _is_main_guard(stmt):
                continue
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                continue  # docstrings and bare constants have no effect
            if isinstance(stmt, ast.Import | ast.ImportFrom) and all(
                alias.name != "*" for alias in stmt.names
            ):
                for alias in stmt.names:  # each imported name is its own symbol
                    single = ast.Import(names=[alias])
                    if isinstance(stmt, ast.ImportFrom):
                        single = ast.ImportFrom(module=stmt.module, names=[alias], level=stmt.level)
                    bound = alias.asname or alias.name
                    bound = bound if isinstance(stmt, ast.ImportFrom) else bound.split(".")[0]
                    symbols.setdefault(bound, []).append(single)
                continue
            binder = _Binder()
            binder.visit(stmt)
            if binder.star:
                reason = reason or "from ... import *"
            if not binder.names:
                preamble.append(stmt)
            for name in binder.names:
                symbols.setdefault(name, []).append(stmt)
        stages, packages = _stage_aliases(tree)
        return cls(tree, symbols, preamble, reason, stage_names=stages, varve_packages=packages)

    def hash_of(self, nodes: Iterable[ast.AST]) -> str:
        parts = []
        for node in nodes:
            digest = self.hashes.get(id(node))
            if digest is None:
                digest = _stmt_hash(self._hashable(node))
                self.hashes[id(node)] = digest
            parts.append(digest)
        return json_hash(parts)


def _locate_function(
    tree: ast.Module, func: Callable[..., Any]
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.ClassDef | None]:
    code = getattr(func, "__code__", None)
    if code is None:
        raise ValueError(f"Cannot locate the source of {func!r}")
    first = code.co_firstlineno
    matches: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, ast.ClassDef | None]] = []
    for parent in ast.walk(tree):
        body = getattr(parent, "body", ())
        if not isinstance(body, list):  # a lambda's body is a single expression
            continue
        for child in body:
            if (
                isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef)
                and child.name == func.__name__
                and min((d.lineno for d in child.decorator_list), default=child.lineno)
                <= first
                <= (child.end_lineno or child.lineno)
            ):
                matches.append((child, parent if isinstance(parent, ast.ClassDef) else None))
    if len(matches) != 1:
        raise ValueError(
            f"Cannot uniquely locate the AST of {func.__qualname__} at line {first} "
            f"(found {len(matches)} candidates)"
        )
    return matches[0]


def _stage_classes(
    module: _Module, owner: ast.ClassDef | None, pipeline: type[Any]
) -> list[ast.ClassDef]:
    """The stage's defining class and, when different and in this file, the pipeline class."""

    classes = [owner] if owner is not None else []
    if "." not in pipeline.__qualname__ and (owner is None or owner.name != pipeline.__name__):
        for stmt in module.tree.body:
            if isinstance(stmt, ast.ClassDef) and stmt.name == pipeline.__name__:
                classes.append(stmt)
                break
    return classes


def _class_members(node: ast.ClassDef) -> dict[str, list[ast.stmt]]:
    members: dict[str, list[ast.stmt]] = {}
    for stmt in node.body:
        binder = _Binder()
        binder.visit(stmt)
        for name in binder.names:
            members.setdefault(name, []).append(stmt)
    return members


def _excluded_names(pipeline: type[Any], path: Path) -> set[str]:
    names: set[str] = set()
    for attr in ("Config", "Args"):
        value = getattr(pipeline, attr, None)
        if not isinstance(value, type) or "." in value.__qualname__:
            continue
        try:
            source = inspect.getsourcefile(value)
        except TypeError:
            continue
        if source is not None and Path(source).resolve() == path:
            names.add(value.__name__)
    return names


class SourceAnalyzer:
    """Computes stage ``source_fp`` values; caches parsed files within one command.

    Cross-file sources hash through ``session`` (and so through its
    ``StatCache``, which the engine backs with the ``stat_cache`` table).
    """

    def __init__(self, session: FingerprintSession | None = None) -> None:
        self.session = session or FingerprintSession()
        self._modules: dict[Path, tuple[tuple[int, int, int], _Module]] = {}

    def _module(self, path: Path) -> _Module:
        st = path.stat()
        token = (st.st_ino, st.st_size, st.st_mtime_ns)
        cached = self._modules.get(path)
        if cached is not None and cached[0] == token:
            return cached[1]
        module = _Module.parse(path, path.read_bytes())
        self._modules[path] = (token, module)
        return module

    def stage_source(
        self,
        pipeline: type[Any],
        stage: str,
        sources: Sequence[Path | str] = (),
    ) -> SourceFingerprint:
        """``source_fp`` of ``stage``: its closure in its file plus declared ``sources``.

        Relative ``sources`` resolve against the pipeline module's directory.
        """

        func = pipeline.stages()[stage].func
        source_file = inspect.getsourcefile(func)
        if source_file is None:
            raise ValueError(f"Cannot locate the source file of stage {stage!r}")
        path = Path(source_file).resolve()
        base = Path(pipeline.module_file()).parent
        module = self._module(path)
        fallback = module.fallback_reason
        if fallback is None:
            node, owner = _locate_function(module.tree, func)
            classes = _stage_classes(module, owner, pipeline)
            fallback = next(
                (reason for item in classes if (reason := _class_dynamic_lookup(item))), None
            )
        if fallback is not None:
            symbols = {WHOLE_FILE: self.session.ast_hash(path)}
            mode = "whole-file"
        else:
            excluded = _excluded_names(pipeline, path)
            stage_funcs = {spec.func.__name__ for spec in pipeline.stages().values()}
            symbols = self._closure(module, node, owner, classes, excluded, stage_funcs)
            mode = "closure"
        external = self._external(sources, base)
        fingerprint = json_hash({"mode": mode, "symbols": symbols, "external": external})
        detail = {
            "file": str(path),
            "mode": mode,
            "fallback_reason": fallback,
            "symbols": symbols,
            "external": external,
        }
        return SourceFingerprint(fingerprint, detail)

    def _closure(
        self,
        module: _Module,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        owner: ast.ClassDef | None,
        classes: Sequence[ast.ClassDef],
        excluded: set[str],
        stage_funcs: set[str],
    ) -> dict[str, str]:
        class_name = owner.name if owner is not None else None
        members = _class_members(owner) if owner is not None else {}
        prefix = f"{class_name}." if class_name else ""
        symbols: dict[str, str] = {f"{prefix}{node.name}": module.hash_of([node])}
        pending_names, pending_members = _references(node, class_name)
        seen_members = {node.name, "Config", "Args"}

        def add(
            key: str, statements: Sequence[ast.AST], scope: str | None, body: bool = False
        ) -> None:
            symbols[key] = module.hash_of(statements)
            for stmt in statements:
                names, reached = _references(stmt, scope)
                pending_names.update(names)
                if scope == class_name:
                    pending_members.update(reached - seen_members)
                    if body:  # bare names in a class body resolve to class members
                        pending_members.update((names & members.keys()) - seen_members)

        for item in classes:
            scope = item.name
            add(f"{scope}.<header>", [*item.bases, *item.keywords, *item.decorator_list], scope)
            for index, stmt in enumerate(item.body):
                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                    continue  # docstrings
                binder = _Binder()
                binder.visit(stmt)
                if binder.names and binder.names <= {"Config", "Args"}:
                    continue
                if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) and not (
                    stmt.name.startswith("__")
                    and stmt.name.endswith("__")
                    and stmt.name not in stage_funcs
                    and not module.stage_decorated(stmt)
                ):
                    # Stage and helper methods are only reached through references;
                    # dunder methods (``__init__``, ...) run for every stage.
                    continue
                label = ",".join(sorted(binder.names)) or f"<body:{index}>"
                if scope == class_name:
                    seen_members.update(binder.names)
                if label == "defaults":  # scheduling keys are stripped like in `@stage(...)`
                    stmt = module.class_defaults(stmt)
                add(f"{scope}.{label}", [stmt], scope, body=True)
            mutations = [
                stmt for stmt in module.symbols.get(scope, ()) if not isinstance(stmt, ast.ClassDef)
            ]
            if mutations:  # module-level `ClassName.attr = ...`
                add(f"{scope}.<module>", mutations, scope)
        if module.preamble:
            add(PREAMBLE, module.preamble, None)
        own = {item.name for item in classes}
        seen_names: set[str] = set()
        while pending_names or pending_members:
            if pending_members:
                member = pending_members.pop()
                if member in seen_members or member not in members:
                    continue
                seen_members.add(member)
                add(f"{prefix}{member}", members[member], class_name)
            else:
                name = pending_names.pop()
                if name in seen_names or name in own or name in excluded:
                    continue
                seen_names.add(name)
                statements = module.symbols.get(name)
                if statements is None:
                    continue  # builtin, local, or unknown name
                add(name, statements, class_name)
        return dict(sorted(symbols.items()))

    def _external(self, sources: Sequence[Path | str], base: Path) -> dict[str, str]:
        external: dict[str, str] = {}
        for item in sources:
            declared = Path(item).expanduser()
            declared = declared if declared.is_absolute() else base / declared
            label_root = os.path.relpath(declared, base)
            files = self.session.walk(declared)
            if declared.expanduser().resolve().is_file():
                if declared.suffix != ".py":
                    raise ValueError(f"Declared source file must end in .py: {declared}")
                external[Path(label_root).as_posix()] = self.session.ast_hash(files[0][1])
                continue
            for rel, path in files:
                if rel.endswith(".py"):
                    label = Path(label_root, rel).as_posix()
                    external[label] = self.session.ast_hash(path)
        return dict(sorted(external.items()))
