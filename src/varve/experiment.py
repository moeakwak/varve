"""Experiment base class and stage graph collection."""

from __future__ import annotations

from functools import cache
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Any, ClassVar

from varve.decorators import StageSpec


class Experiment:
    """Base class for varve experiments.

    Subclasses declare stage methods with `@stage` and `@batch_stage`. The base
    class keeps orchestration outside experiment code by collecting and sorting
    those declarations.
    """

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
    def default_output_root(cls, config: Any) -> Path:
        raise NotImplementedError(f"{cls.__name__} must override default_output_root")

    @classmethod
    def resolve_output_root(cls, base: Path, config: Any) -> Path:
        return base

    @classmethod
    def output_root(cls, config: Any, *, cli_out: Path | None = None) -> Path:
        base = Path(cli_out) if cli_out is not None else cls.default_output_root(config)
        return cls.resolve_output_root(base, config)

    @classmethod
    def clean_roots(cls, config: Any) -> list[Path] | None:
        """Optionally restrict full-clean (no target) to specific roots.

        Returning None (default) keeps current behavior: only the dangerous blacklist
        (/, home, cwd) and manifest anchor guard a full clean. Override to declare
        experiment-specific allowed roots (e.g. studies/results, /tmp).
        """
        return None

    @classmethod
    def cli(cls, argv: list[str] | None = None) -> int:
        from varve.cli.app import main

        return main(cls, argv)
