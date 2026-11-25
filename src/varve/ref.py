"""Cross-pipeline and cross-branch references.

This module only declares :class:`Ref`; the default resolver
(:mod:`varve.workspace.refs`) reads the upstream ``state.db`` without
importing the upstream module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Ref:
    """A committed stage of another pipeline (or branch), injected like an upstream stage.

    ``target`` is a Pipeline class or a zero-import selector such as
    ``"dataset_audit.label_renderability"`` (full name, module, or unambiguous
    dotted suffix of a ``varve.toml`` pipeline or a discovered store). Only
    ``ok`` units of ``stage`` are read; their ``artifact_fp`` enters the
    consuming unit's key. Axes align by name: shared names align 1-to-1 (a
    missing upstream coordinate is an error), upstream-only axes fan in as a
    ``dict`` keyed by upstream id (a tuple of ids for several axes).
    ``align_by=()`` fans in everything, ``align_by=("a",)`` aligns only on the
    listed names, a mapping renames ``{local_axis: upstream_axis}``. Several
    ``branch`` names add an outer ``dict`` keyed by branch. ``selector``
    (``"axis=id,..."``) filters the upstream units first.
    """

    target: type[Any] | str
    stage: str
    branch: str | Sequence[str] = "main"
    selector: str | None = None
    align_by: Sequence[str] | Mapping[str, str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.branch, str):
            object.__setattr__(self, "branch", tuple(self.branch))
        if self.align_by is not None and not isinstance(self.align_by, Mapping):
            object.__setattr__(self, "align_by", tuple(self.align_by))

    @property
    def branches(self) -> tuple[str, ...]:
        return (self.branch,) if isinstance(self.branch, str) else tuple(self.branch)

    @property
    def multi_branch(self) -> bool:
        return not isinstance(self.branch, str)

    def describe(self) -> str:
        target = self.target if isinstance(self.target, str) else self.target.__qualname__
        return f"{target}:{self.stage}"
