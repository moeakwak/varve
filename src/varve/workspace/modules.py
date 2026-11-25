"""Locate pipeline modules without importing them, and import them on demand."""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from importlib.machinery import PathFinder
from pathlib import Path
from types import ModuleType

from varve.pipeline import Pipeline
from varve.selector import selector_from_module


@dataclass(frozen=True)
class ModuleLocation:
    module: str
    file: Path | None
    """``__init__.py`` / module file (``None`` for namespace packages)."""
    directory: Path
    """The package directory, or the module file's directory."""
    is_package: bool
    has_main: bool
    """A package with ``__main__.py`` (``python -m <module>`` works)."""


def locate_module(module: str, search: list[str] | None = None) -> ModuleLocation | None:
    """Find ``module`` on ``sys.path`` (or ``search``) without executing any package.

    Already imported packages are reused; otherwise each level is looked up
    with ``PathFinder`` against the parent's search locations, so no parent
    ``__init__`` runs.
    """

    parts = module.split(".")
    path: list[str] | None = search
    origin: str | None = None
    locations: list[str] | None = None
    for index in range(len(parts)):
        name = ".".join(parts[: index + 1])
        loaded = sys.modules.get(name)
        if loaded is not None and getattr(loaded, "__spec__", None) is not None:
            origin = getattr(loaded, "__file__", None)
            package_path = getattr(loaded, "__path__", None)
            locations = list(package_path) if package_path is not None else None
        else:
            try:
                spec = PathFinder.find_spec(parts[index], path)
            except (ImportError, ValueError):
                return None
            if spec is None:
                return None
            origin = spec.origin if spec.has_location else None
            locations = (
                list(spec.submodule_search_locations)
                if spec.submodule_search_locations is not None
                else None
            )
        if index < len(parts) - 1:
            if locations is None:
                return None
            path = locations
    is_package = locations is not None
    file = Path(origin).absolute() if origin else None
    if file is not None:
        directory = file.parent
    elif locations:
        directory = Path(locations[0]).absolute()
    else:
        return None
    has_main = is_package and (directory / "__main__.py").is_file()
    return ModuleLocation(module, file, directory, is_package, has_main)


def split_entry(entry: str) -> tuple[str, str | None]:
    """``"pkg.mod:Class"`` -> ``("pkg.mod", "Class")``; a bare module has no qualname."""

    module, _, qualname = entry.partition(":")
    return module, qualname or None


def entry_selector(entry: str, location: ModuleLocation | None) -> str:
    """Zero-import selector of a registered entry.

    A package entry is its own selector (its pipeline module lives inside it);
    a module entry is addressed by its package, like ``Pipeline.selector()``.
    ``varve_name`` overrides are only visible once the pipeline has a store.
    """

    module, _ = split_entry(entry)
    if location is not None and location.is_package:
        return module
    return selector_from_module(module)


def ensure_on_path(directory: Path | None) -> None:
    """Make modules below ``directory`` (the workspace root) importable."""

    if directory is None:
        return
    text = str(directory)
    if text not in sys.path:
        sys.path.insert(0, text)


def load_pipeline(module: str, qualname: str | None = None) -> type[Pipeline]:
    """Import ``module`` and return its Pipeline class.

    Without ``qualname`` the module namespace is scanned for Pipeline
    subclasses that declare stages; for a package without one, its
    ``__main__`` submodule is scanned too. Several distinct candidates are an
    error asking for ``module:Class``.
    """

    loaded = importlib.import_module(module)
    if qualname is not None:
        value: object = loaded
        for part in qualname.split("."):
            value = getattr(value, part, None)
        if not (isinstance(value, type) and issubclass(value, Pipeline)):
            raise ValueError(f"{module}:{qualname} is not a varve Pipeline class")
        return value
    candidates = _candidates(loaded)
    if not candidates and hasattr(loaded, "__path__"):
        try:
            main = importlib.import_module(f"{module}.__main__")
        except ModuleNotFoundError as error:
            if error.name != f"{module}.__main__":
                raise
        else:
            candidates = _candidates(main)
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ValueError(f"Module {module!r} defines no varve Pipeline with stages")
    names = ", ".join(f"{cls.__module__}:{cls.__qualname__}" for cls in candidates)
    raise ValueError(
        f"Module {module!r} has several Pipeline classes ({names}); "
        f"register one as '{module}:<Class>'"
    )


def _candidates(module: ModuleType) -> list[type[Pipeline]]:
    found: list[type[Pipeline]] = []
    for value in vars(module).values():
        if (
            isinstance(value, type)
            and issubclass(value, Pipeline)
            and value is not Pipeline
            and value not in found
            and value.stages()
        ):
            found.append(value)
    # Prefer the most derived classes, and those defined in (or below) the module.
    found = [cls for cls in found if not any(o is not cls and issubclass(o, cls) for o in found)]
    base = module.__name__.removesuffix(".__main__")
    local = [
        cls for cls in found if cls.__module__ == base or cls.__module__.startswith(base + ".")
    ]
    return local or found
