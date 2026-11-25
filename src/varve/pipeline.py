"""Pipeline base class: declarations, roots, and graph access."""

from __future__ import annotations

import importlib
import importlib.util
import sys
from collections.abc import Mapping
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel

from varve.decorators import StageSpec
from varve.ref import Ref
from varve.selector import selector_from_module, validate_selector

if TYPE_CHECKING:
    from varve.graph import StageGraph


class _EmptyConfig(BaseModel):
    pass


class _EmptyArgs(BaseModel):
    pass


class Pipeline:
    """Base class for varve pipelines.

    Subclasses declare ``@stage`` methods, optionally ``Config`` (durable
    settings that enter keys), ``Args`` (runtime-only options), ``defaults``
    (stage defaults: storage, resources, depends, workers, backend,
    max_failures), and ``Ref`` class attributes for cross-pipeline inputs.
    """

    Config: ClassVar[type[Any]] = _EmptyConfig
    Args: ClassVar[type[BaseModel]] = _EmptyArgs
    defaults: ClassVar[Mapping[str, Any]] = {}
    # Overrides the selector `varve` displays and accepts (default: the package
    # that holds the defining module).
    varve_name: ClassVar[str | None] = None

    @classmethod
    @cache
    def stages(cls) -> dict[str, StageSpec]:
        """Stage declarations in definition order (base classes first)."""

        collected: dict[str, StageSpec] = {}
        for base in reversed(cls.__mro__):
            for attr, value in base.__dict__.items():
                spec = getattr(value, "__varve_stage__", None)
                if not isinstance(spec, StageSpec):
                    collected.pop(attr, None)  # overridden by a non-stage attribute
                    continue
                if attr in Pipeline.__dict__:
                    raise ValueError(f"Stage name {attr!r} shadows a Pipeline attribute")
                if attr != spec.name:
                    raise ValueError(
                        f"Stage {spec.name!r} is bound to attribute {attr!r}; "
                        "a stage must keep its function name"
                    )
                collected[attr] = spec
        return collected

    @classmethod
    @cache
    def refs(cls) -> dict[str, Ref]:
        """``Ref`` class attributes by attribute name."""

        refs: dict[str, Ref] = {}
        for base in reversed(cls.__mro__):
            for attr, value in base.__dict__.items():
                if isinstance(value, Ref):
                    refs[attr] = value
        return refs

    @classmethod
    @cache
    def graph(cls) -> StageGraph:
        """The validated logical stage graph (raises ``GraphError`` on bad declarations)."""

        from varve.graph import build_graph

        return build_graph(cls)

    @classmethod
    def import_module_name(cls) -> str:
        """Module name a child process can import (``__main__`` resolves to its spec name)."""

        if cls.__module__ != "__main__":
            return cls.__module__
        spec = getattr(sys.modules.get("__main__"), "__spec__", None)
        return getattr(spec, "name", None) or cls.__module__

    @classmethod
    def selector(cls) -> str:
        """The user-facing selector the `varve` command displays and accepts."""

        if cls.varve_name is not None:
            return validate_selector(cls.varve_name)
        module = cls.import_module_name()
        if module == "__main__":
            main_file = getattr(sys.modules.get("__main__"), "__file__", None)
            return Path(main_file).stem if main_file is not None else module
        return selector_from_module(module)

    @classmethod
    def module_file(cls) -> Path:
        module = sys.modules.get(cls.__module__)
        module_file = getattr(module, "__file__", None)
        if module_file is None:
            spec = importlib.util.find_spec(cls.import_module_name())
            module_file = getattr(spec, "origin", None)
        if module_file is None:
            raise ValueError(f"Cannot locate the module file of {cls.__qualname__}")
        return Path(module_file).absolute()

    @classmethod
    def default_root(cls, config: Any) -> Path:
        """Pipeline root holding ``.varve/`` (default: the defining module's directory)."""

        return cls.module_file().parent

    @classmethod
    def storage_roots(cls, config: Any) -> Mapping[str, Path]:
        """Named storage roots; relative paths resolve against the pipeline root.

        ``"default"`` falls back to ``<root>/out`` when not declared here or in
        the workspace ``varve.toml``.
        """

        return {}

    @classmethod
    def publish_root(cls, config: Any) -> Path | None:
        """Root of published deliverables (``None``: the ``default`` storage root)."""

        return None

    @classmethod
    def varve_config_path(cls) -> Path | None:
        path = cls.module_file().parent / "varve.yaml"
        return path if path.exists() else None

    @classmethod
    def cli(cls, argv: list[str] | None = None) -> int:
        """Run the generated command-line interface."""

        app = importlib.import_module("varve.cli.app")
        return app.main(cls, argv)
