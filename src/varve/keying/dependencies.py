"""Best-effort discovery of bounded Python source dependencies."""

from __future__ import annotations

import dis
import inspect
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import CodeType, ModuleType
from typing import Any, Literal

from varve.keying.astkey import _normalized_source_hash
from varve.keying.fingerprint import json_sha256

logger = logging.getLogger(__name__)

DependencyKind = Literal["function", "class", "module", "value"]
DependencyOrigin = Literal["inferred", "explicit"]

_STAGE_ROOT = "stage"
_UNSUPPORTED = object()


@dataclass(frozen=True)
class _CallableInspection:
    digest: str
    source_path: str | None
    source_line: int


@dataclass(frozen=True)
class _InstructionReference:
    kind: Literal["global", "closure"]
    name: str
    attribute: str | None = None


@dataclass
class SourceInspectionSession:
    """Cache static Python inspection for one command without caching bindings."""

    callables: dict[Callable[..., Any], _CallableInspection] = field(default_factory=dict)
    modules: dict[ModuleType, tuple[str, str | None]] = field(default_factory=dict)
    instructions: dict[CodeType, tuple[_InstructionReference, ...]] = field(default_factory=dict)
    classes: dict[
        type[Any], tuple[tuple[tuple[str, Callable[..., Any]], ...], tuple[type[Any], ...]]
    ] = field(default_factory=dict)

    def inspect_callable(self, value: Callable[..., Any]) -> _CallableInspection:
        cached = self.callables.get(value)
        if cached is not None:
            return cached
        try:
            lines, line = inspect.getsourcelines(value)
        except OSError as error:
            raise ValueError(f"Cannot inspect source for {value!r}") from error
        try:
            path = inspect.getsourcefile(value)
        except (OSError, TypeError):
            path = None
        result = _CallableInspection(
            digest=_normalized_source_hash(
                "".join(lines),
                strip_varve_decorators=hasattr(value, "__varve_stage__"),
            ),
            source_path=path,
            source_line=line,
        )
        self.callables[value] = result
        return result

    def inspect_module(self, module: ModuleType) -> tuple[str, str | None]:
        cached = self.modules.get(module)
        if cached is not None:
            return cached
        source_path = inspect.getsourcefile(module)
        if source_path is None:
            raise ValueError(f"Cannot locate source for module {module.__name__}")
        source = Path(source_path).read_text(encoding="utf-8")
        result = (_normalized_source_hash(source), source_path)
        self.modules[module] = result
        return result

    def instruction_plan(self, code: CodeType) -> tuple[_InstructionReference, ...]:
        cached = self.instructions.get(code)
        if cached is not None:
            return cached
        result: list[_InstructionReference] = []
        for nested in nested_codes(code):
            instructions = tuple(dis.get_instructions(nested))
            for index, instruction in enumerate(instructions):
                if instruction.opname in {"LOAD_GLOBAL", "LOAD_NAME"}:
                    following = instructions[index + 1] if index + 1 < len(instructions) else None
                    attribute = (
                        str(following.argval)
                        if following is not None
                        and following.opname in {"LOAD_ATTR", "LOAD_METHOD"}
                        else None
                    )
                    result.append(
                        _InstructionReference("global", str(instruction.argval), attribute)
                    )
                elif instruction.opname == "LOAD_DEREF":
                    result.append(_InstructionReference("closure", str(instruction.argval)))
        planned = tuple(result)
        self.instructions[code] = planned
        return planned

    def inspect_class(
        self, cls: type[Any]
    ) -> tuple[tuple[tuple[str, Callable[..., Any]], ...], tuple[type[Any], ...]]:
        cached = self.classes.get(cls)
        if cached is not None:
            return cached
        result = (class_functions(cls), tuple(cls.__bases__))
        self.classes[cls] = result
        return result


@dataclass(frozen=True)
class DependencyNode:
    identity: str
    kind: DependencyKind
    qualified_name: str
    digest: str
    origin: DependencyOrigin
    scope: str | None
    source_path: str | None
    source_line: int | None

    @property
    def component_name(self) -> str:
        prefix = "uses" if self.origin == "explicit" else "auto"
        return f"{prefix}.{self.kind}.{self.qualified_name}"


@dataclass(frozen=True)
class DependencyEdge:
    parent: str
    child: str
    reason: str


@dataclass(frozen=True)
class SourceDependencies:
    components: dict[str, str]
    nodes: dict[str, DependencyNode]
    edges: tuple[DependencyEdge, ...]
    direct: tuple[str, ...]
    diagnostics: tuple[str, ...] = ()

    def component_names(self) -> set[str]:
        return set(self.components)

    def find(self, qualified_name: str) -> DependencyNode | None:
        return next(
            (node for node in self.nodes.values() if node.qualified_name == qualified_name),
            None,
        )

    def with_component(self, name: str, digest: str) -> SourceDependencies:
        return replace(
            self,
            components=dict(sorted({name: digest, **self.components}.items())),
        )


@dataclass(frozen=True)
class Reference:
    locator: str
    value: Any
    reason: str
    module: ModuleType | None = None


def module_name(value: Any) -> str:
    module = getattr(value, "__module__", "")
    if isinstance(value, ModuleType):
        module = value.__name__
    if module != "__main__":
        return module
    main_module = sys.modules.get("__main__")
    spec_name = getattr(getattr(main_module, "__spec__", None), "name", None)
    return spec_name or "__main__"


def default_packages(func: Callable[..., Any]) -> tuple[str, ...]:
    module = module_name(func)
    if module == "__main__":
        return ("__main__",)
    return (module.split(".", 1)[0],) if module else ()


def in_packages(value: Any, packages: tuple[str, ...]) -> bool:
    module = module_name(value)
    return any(module == package or module.startswith(f"{package}.") for package in packages)


def qualified_name(value: Any) -> str:
    module = module_name(value)
    name = getattr(value, "__qualname__", getattr(value, "__name__", type(value).__name__))
    return f"{module}.{name}" if module else name


def nested_codes(root: CodeType) -> tuple[CodeType, ...]:
    result = [root]
    for constant in root.co_consts:
        if isinstance(constant, CodeType):
            result.extend(nested_codes(constant))
    return tuple(result)


def _closure_values(func: Callable[..., Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for name, cell in zip(
        getattr(func.__code__, "co_freevars", ()),
        func.__closure__ or (),
        strict=True,
    ):
        try:
            values[name] = cell.cell_contents
        except ValueError:
            continue
    return values


def function_references(
    func: Callable[..., Any],
    inspection: SourceInspectionSession | None = None,
) -> tuple[Reference, ...]:
    inspection = inspection or SourceInspectionSession()
    references: list[Reference] = []
    globals_dict = getattr(func, "__globals__", {})
    owner = qualified_name(func)
    closure_values = _closure_values(func)

    for reference in inspection.instruction_plan(func.__code__):
        if reference.kind == "global":
            name = reference.name
            if name not in globals_dict:
                continue
            value = globals_dict[name]
            if isinstance(value, ModuleType) and reference.attribute is not None:
                attribute = reference.attribute
                references.append(
                    Reference(
                        locator=f"auto.value.{value.__name__}.attr.{attribute}",
                        value=getattr(value, attribute, _UNSUPPORTED),
                        reason=f"module attribute referenced by {owner}",
                        module=value,
                    )
                )
            else:
                references.append(
                    Reference(
                        locator=f"auto.value.{owner}.global.{name}",
                        value=value,
                        reason=f"global referenced by {owner}",
                    )
                )
        else:
            name = reference.name
            if name in closure_values:
                references.append(
                    Reference(
                        locator=f"auto.value.{owner}.closure.{name}",
                        value=closure_values[name],
                        reason=f"closure referenced by {owner}",
                    )
                )

    for name, parameter in inspect.signature(func).parameters.items():
        if parameter.default is inspect.Parameter.empty:
            continue
        references.append(
            Reference(
                locator=f"auto.value.{owner}.default.{name}",
                value=parameter.default,
                reason=f"default value declared by {owner}",
            )
        )
    return tuple(references)


def stable_value(value: Any) -> Any:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, list | tuple):
        items = [stable_value(item) for item in value]
        return _UNSUPPORTED if any(item is _UNSUPPORTED for item in items) else items
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        items = {key: stable_value(item) for key, item in sorted(value.items())}
        return _UNSUPPORTED if any(item is _UNSUPPORTED for item in items.values()) else items
    return _UNSUPPORTED


def class_functions(cls: type[Any]) -> tuple[tuple[str, Callable[..., Any]], ...]:
    result: list[tuple[str, Callable[..., Any]]] = []
    for name, member in cls.__dict__.items():
        if inspect.isfunction(member):
            result.append((name, member))
        elif isinstance(member, staticmethod | classmethod):
            result.append((name, member.__func__))
        elif isinstance(member, property):
            for accessor_name, accessor in (
                (f"{name}.fget", member.fget),
                (f"{name}.fset", member.fset),
                (f"{name}.fdel", member.fdel),
            ):
                if accessor is not None:
                    result.append((accessor_name, accessor))
    return tuple(result)


class _Builder:
    def __init__(
        self,
        packages: tuple[str, ...],
        stage_func: Callable[..., Any],
        inspection: SourceInspectionSession,
    ) -> None:
        self.packages = packages
        self.inspection = inspection
        self.stage_identity = self._identity(stage_func)
        self.components: dict[str, str] = {}
        self.nodes: dict[str, DependencyNode] = {}
        self.edges: set[DependencyEdge] = set()
        self.direct: set[str] = set()
        self.scanned: set[str] = set()

    def _connect(self, parent: str, child: str, reason: str, *, direct: bool) -> None:
        self.edges.add(DependencyEdge(parent=parent, child=child, reason=reason))
        if direct:
            self.direct.add(child)

    def add_explicit(self, value: Callable[..., Any]) -> None:
        if not callable(value):
            raise TypeError(f"Explicit varve uses must be callable: {value!r}")
        kind: DependencyKind = "class" if inspect.isclass(value) else "function"
        scope = "whole class" if kind == "class" else None
        name = qualified_name(value)
        identity = f"{kind}:{name}"
        inspected = self.inspection.inspect_callable(value)
        self.nodes[identity] = DependencyNode(
            identity=identity,
            kind=kind,
            qualified_name=name,
            digest=inspected.digest,
            origin="explicit",
            scope=scope,
            source_path=inspected.source_path,
            source_line=inspected.source_line,
        )
        self.components[f"uses.{kind}.{name}"] = inspected.digest
        self._connect(_STAGE_ROOT, identity, "declared by uses", direct=True)

    def add_inferred_from(self, value: Callable[..., Any], *, stage_root: bool = False) -> None:
        parent = _STAGE_ROOT if stage_root else self._identity(value)
        if inspect.isclass(value):
            self._scan_class(value, parent=parent, direct=stage_root)
        else:
            self._scan_function(value, parent=parent, direct=stage_root)

    def _identity(self, value: Any) -> str:
        kind = "class" if inspect.isclass(value) else "function"
        return f"{kind}:{qualified_name(value)}"

    def _scan_function(
        self,
        func: Callable[..., Any],
        *,
        parent: str,
        direct: bool,
    ) -> None:
        identity = self._identity(func)
        if identity in self.scanned:
            return
        self.scanned.add(identity)
        try:
            references = function_references(func, self.inspection)
        except Exception:
            logger.debug("auto_uses could not inspect %s", qualified_name(func), exc_info=True)
            return
        for reference in references:
            try:
                self._add_reference(parent, reference, direct=direct)
            except Exception:
                logger.debug(
                    "auto_uses skipped a reference from %s",
                    qualified_name(func),
                    exc_info=True,
                )

    def _scan_class(self, cls: type[Any], *, parent: str, direct: bool) -> None:
        identity = self._identity(cls)
        if identity in self.scanned:
            return
        self.scanned.add(identity)
        functions, bases = self.inspection.inspect_class(cls)
        for _, func in functions:
            try:
                references = function_references(func, self.inspection)
            except Exception:
                logger.debug("auto_uses could not inspect %s", qualified_name(func), exc_info=True)
                continue
            for reference in references:
                try:
                    self._add_reference(parent, reference, direct=direct)
                except Exception:
                    logger.debug(
                        "auto_uses skipped a reference from %s",
                        qualified_name(func),
                        exc_info=True,
                    )
        for base in bases:
            if base is object:
                continue
            self._add_inferred_object(
                base,
                parent=parent,
                reason=f"base class of {qualified_name(cls)}",
                direct=direct,
            )

    def _add_reference(self, parent: str, reference: Reference, *, direct: bool) -> None:
        value = reference.value
        if value is _UNSUPPORTED:
            return
        if inspect.isfunction(value) or inspect.isclass(value):
            self._add_inferred_object(
                value,
                parent=parent,
                reason=reference.reason,
                direct=direct,
            )
            return
        if isinstance(value, ModuleType):
            self._add_module(value, parent=parent, reason=reference.reason, direct=direct)
            return
        normalized = stable_value(value)
        if normalized is not _UNSUPPORTED:
            self._add_value(
                reference.locator,
                normalized,
                parent=parent,
                reason=reference.reason,
                direct=direct,
            )
            return
        if reference.module is not None:
            self._add_module(
                reference.module,
                parent=parent,
                reason=reference.reason,
                direct=direct,
            )

    def _add_inferred_object(
        self,
        value: Any,
        *,
        parent: str,
        reason: str,
        direct: bool,
    ) -> None:
        if not in_packages(value, self.packages):
            return
        kind: DependencyKind = "class" if inspect.isclass(value) else "function"
        name = qualified_name(value)
        identity = f"{kind}:{name}"
        if identity == self.stage_identity and identity not in self.nodes:
            return
        if identity not in self.nodes:
            try:
                inspected = self.inspection.inspect_callable(value)
            except Exception:
                logger.debug("auto_uses could not hash %s", name, exc_info=True)
                return
            self.nodes[identity] = DependencyNode(
                identity=identity,
                kind=kind,
                qualified_name=name,
                digest=inspected.digest,
                origin="inferred",
                scope="whole class" if kind == "class" else None,
                source_path=inspected.source_path,
                source_line=inspected.source_line,
            )
            self.components[f"auto.{kind}.{name}"] = inspected.digest
        self._connect(parent, identity, reason, direct=direct)
        if inspect.isclass(value):
            self._scan_class(value, parent=identity, direct=False)
        else:
            self._scan_function(value, parent=identity, direct=False)

    def _add_module(
        self,
        module: ModuleType,
        *,
        parent: str,
        reason: str,
        direct: bool,
    ) -> None:
        if not in_packages(module, self.packages):
            return
        name = module.__name__
        identity = f"module:{name}"
        if identity not in self.nodes:
            try:
                digest, path = self.inspection.inspect_module(module)
            except Exception:
                logger.debug("auto_uses could not hash module %s", name, exc_info=True)
                return
            self.nodes[identity] = DependencyNode(
                identity=identity,
                kind="module",
                qualified_name=name,
                digest=digest,
                origin="inferred",
                scope="module file",
                source_path=path,
                source_line=None,
            )
            self.components[f"auto.module.{name}"] = digest
        self._connect(parent, identity, reason, direct=direct)

    def _add_value(
        self,
        locator: str,
        value: Any,
        *,
        parent: str,
        reason: str,
        direct: bool,
    ) -> None:
        try:
            digest = json_sha256(value)
        except (TypeError, ValueError):
            return
        identity = f"value:{locator}"
        if identity not in self.nodes:
            self.nodes[identity] = DependencyNode(
                identity=identity,
                kind="value",
                qualified_name=locator.removeprefix("auto.value."),
                digest=digest,
                origin="inferred",
                scope=None,
                source_path=None,
                source_line=None,
            )
            self.components[locator] = digest
        self._connect(parent, identity, reason, direct=direct)

    def finish(self) -> SourceDependencies:
        return SourceDependencies(
            components=dict(sorted(self.components.items())),
            nodes=dict(sorted(self.nodes.items())),
            edges=tuple(
                sorted(self.edges, key=lambda edge: (edge.parent, edge.child, edge.reason))
            ),
            direct=tuple(sorted(self.direct)),
        )


def discover_source_dependencies(
    stage_func: Callable[..., Any],
    *,
    explicit_uses: tuple[Callable[..., Any], ...],
    auto_uses: bool,
    packages: tuple[str, ...] | None,
    inspection: SourceInspectionSession | None = None,
) -> SourceDependencies:
    resolved_packages = default_packages(stage_func) if packages is None else packages
    builder = _Builder(resolved_packages, stage_func, inspection or SourceInspectionSession())
    for explicit in explicit_uses:
        builder.add_explicit(explicit)
    if auto_uses:
        try:
            builder.add_inferred_from(stage_func, stage_root=True)
            for explicit in explicit_uses:
                builder.add_inferred_from(explicit)
        except Exception:
            logger.debug("auto_uses discovery failed", exc_info=True)
    return builder.finish()
