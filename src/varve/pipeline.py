"""Pipeline base class and stage graph collection."""

from __future__ import annotations

import importlib.util
import sys
from functools import cache
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Any, ClassVar

from pydantic import BaseModel

from varve.branch import validate_branch_name
from varve.decorators import StageSpec


class _EmptyArgs(BaseModel):
    pass


class Pipeline:
    """Base class for varve pipelines.

    Subclasses declare stage methods with `@stage` and `@batch_stage`. The base
    class keeps orchestration outside pipeline code by collecting and sorting
    those declarations.
    """

    Args: ClassVar[type[Any]] = _EmptyArgs
    Config: ClassVar[type[Any]]

    @classmethod
    @cache
    def stages(cls) -> dict[str, StageSpec]:
        collected: dict[str, StageSpec] = {}
        for base in reversed(cls.__mro__):
            for _, value in base.__dict__.items():
                spec = getattr(value, "__varve_stage__", None)
                if spec is not None:
                    if spec.name in collected and collected[spec.name].func is not spec.func:
                        raise ValueError(f"Duplicate varve stage: {spec.name}")
                    collected[spec.name] = spec

        if not collected:
            raise ValueError(f"{cls.__name__} declares no varve stages")

        missing: dict[str, tuple[str, ...]] = {}
        for spec in collected.values():
            unknown = tuple(name for name in spec.needs if name not in collected)
            if unknown:
                missing[spec.name] = unknown
        if missing:
            details = ", ".join(
                f"{stage_name} needs {sorted(unknown)!r}"
                for stage_name, unknown in sorted(missing.items())
            )
            raise ValueError(f"Unknown varve stage dependencies: {details}")

        return dict(collected)

    @classmethod
    def topo_order(cls) -> list[str]:
        graph = {name: set(spec.needs) for name, spec in cls.stages().items()}
        return list(TopologicalSorter(graph).static_order())

    @classmethod
    def import_module_name(cls) -> str:
        if cls.__module__ != "__main__":
            return cls.__module__
        module = sys.modules.get("__main__")
        spec = getattr(module, "__spec__", None)
        spec_name = getattr(spec, "name", None)
        return spec_name or cls.__module__

    @classmethod
    def _module_file(cls) -> str | None:
        module = sys.modules.get(cls.__module__)
        module_file = getattr(module, "__file__", None)
        if module_file is not None:
            return module_file
        spec = importlib.util.find_spec(cls.import_module_name())
        return getattr(spec, "origin", None)

    @classmethod
    def default_output_root(cls, config: Any) -> Path:
        module_file = cls._module_file()
        if module_file is None:
            raise ValueError(f"Cannot locate module file for {cls.__module__}")
        return Path(module_file).resolve().parent / "out"

    @classmethod
    def varve_config_path(cls) -> Path | None:
        module_file = cls._module_file()
        if module_file is None:
            return None
        path = Path(module_file).resolve().parent / "varve.yaml"
        return path if path.exists() else None

    @classmethod
    def output_root(
        cls,
        config: Any,
        *,
        cli_out: Path | None = None,
        branch: str = "main",
        is_temporary: bool = False,
    ) -> Path:
        validate_branch_name(branch)
        base = Path(cli_out) if cli_out is not None else cls.default_output_root(config)
        return base / ".tmp" / branch if is_temporary else base / branch

    @classmethod
    def clean_roots(cls, config: Any) -> list[Path] | None:
        """Optionally restrict full-clean (no target) to specific roots.

        Returning None (default) keeps current behavior: only the dangerous blacklist
        (/, home, cwd) and manifest anchor guard a full clean. Override to declare
        pipeline-specific allowed roots (for example, pipeline outputs and /tmp).
        """
        return None

    @classmethod
    def cli(cls, argv: list[str] | None = None) -> int:
        from varve.cli.app import main

        return main(cls, argv)
