"""View models for ``status``, ``plan`` and ``inspect`` (no rendering here)."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from varve.engine.probe import StagePlan, UnitPlan
from varve.engine.refs import RefInfo, bound_refs
from varve.engine.session import Session
from varve.keying.keys import SEGMENTS, compute_input_key
from varve.store import layout
from varve.store.repo import UnitRow


@dataclass(frozen=True)
class UnitView:
    unit_key: str
    decision: str
    reason: str | None
    labels: tuple[str, ...]

    @property
    def display(self) -> str:
        return self.unit_key or "-"


@dataclass(frozen=True)
class StageView:
    name: str
    status: str
    """``hit``, ``needs-run``, ``failed`` or ``awaiting``."""
    awaiting: str | None
    total: int
    counts: Mapping[str, int]
    reasons: Mapping[str, int]
    labels: Mapping[str, int]
    deps: tuple[str, ...]
    axes: tuple[tuple[str, str], ...]
    storage: str
    units: tuple[UnitView, ...] = ()
    orphaned: int = 0
    """Stored units no longer produced by a dynamic axis (removed by ``clean --orphans``)."""

    @property
    def hit(self) -> int:
        return self.counts.get("hit", 0)

    def summary(self) -> str:
        """``✓ 12``, ``3/12 hit · 9 needs-run (new)``, ``awaiting (prepare)``."""

        if self.status == "awaiting":
            return f"awaiting ({self.awaiting})"
        if self.status == "hit":
            text = f"hit ({self.total})" if self.total != 1 else "hit"
        else:
            parts = [f"{self.hit}/{self.total} hit"]
            if self.counts.get("needs-run"):
                reasons = ", ".join(f"{name} {count}" for name, count in self.reasons.items())
                parts.append(f"{self.counts['needs-run']} needs-run ({reasons})")
            if self.counts.get("failed"):
                parts.append(f"{self.counts['failed']} failed")
            text = " · ".join(parts)
        if self.labels:
            text += " · " + ", ".join(
                f"{label}" if count == self.total else f"{label} {count}"
                for label, count in self.labels.items()
            )
        if self.orphaned:
            text += f" · orphaned {self.orphaned}"
        return text


@dataclass(frozen=True)
class PipelineView:
    pipeline: str
    branch: str
    is_temp: bool
    root: Path
    stages: tuple[StageView, ...]
    refs: tuple[RefInfo, ...] = ()
    """Upstream records read through cross-pipeline Refs (committed state only)."""

    def stage(self, name: str) -> StageView:
        return next(stage for stage in self.stages if stage.name == name)


def _stage_status(plan: StagePlan) -> str:
    if plan.status == "awaiting":
        return "awaiting"
    counts = plan.counts()
    if counts["failed"]:
        return "failed"
    if counts["needs-run"]:
        return "needs-run"
    return "hit"


def stage_view(plan: StagePlan, *, expand: bool = False, orphaned: int = 0) -> StageView:
    node = plan.node
    reasons: Counter[str] = Counter()
    labels: Counter[str] = Counter()
    for unit in plan.units:
        if unit.decision == "needs-run":
            reasons.update(unit.reasons or ((unit.reason,) if unit.reason else ()))
        labels.update(unit.labels)
    return StageView(
        name=node.name,
        status=_stage_status(plan),
        awaiting=plan.awaiting,
        total=len(plan.units),
        counts=plan.counts(),
        reasons=dict(reasons.most_common()),
        labels=dict(labels.most_common()),
        deps=node.deps,
        axes=tuple((axis.name, "static" if axis.is_static else "dynamic") for axis in node.axes),
        storage=node.storage,
        units=tuple(unit_view(unit) for unit in plan.units) if expand else (),
        orphaned=orphaned,
    )


def unit_view(plan: UnitPlan) -> UnitView:
    reason = ", ".join(plan.reasons) if plan.reasons else plan.reason
    return UnitView(plan.unit_key, plan.decision, reason, plan.labels)


def pipeline_view(
    session: Session, plans: Mapping[str, StagePlan], *, expand: bool = False
) -> PipelineView:
    counts = session.repo.unit_counts(session.branch_id) if session.repo is not None else {}
    return PipelineView(
        pipeline=session.pipeline.selector(),
        branch=session.branch.name,
        is_temp=session.branch.is_temp,
        root=session.root,
        stages=tuple(
            stage_view(plan, expand=expand, orphaned=counts.get(plan.name, {}).get("orphaned", 0))
            for plan in plans.values()
        ),
        refs=tuple(bound_refs(session.refs)),
    )


# ---------------------------------------------------------------------------
# inspect


@dataclass(frozen=True)
class SegmentDiff:
    segment: str
    changed: bool
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceDiff:
    mode: str
    fallback_reason: str | None
    changed: tuple[str, ...]
    added: tuple[str, ...]
    removed: tuple[str, ...]
    symbols: tuple[str, ...]
    external: tuple[str, ...]


@dataclass(frozen=True)
class InspectView:
    stage: str
    unit_key: str
    decision: str
    reason: str | None
    reasons: tuple[str, ...]
    labels: tuple[str, ...]
    row: UnitRow | None
    segments: tuple[SegmentDiff, ...]
    source: SourceDiff
    unit_dir: Path
    artifact_path: Path | None
    log_path: Path | None
    current_components: dict[str, Any] | None = field(default=None, repr=False)


def _diff_mapping(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    lines = []
    for name in sorted(set(old) | set(new)):
        if name not in old:
            lines.append(f"+ {name}")
        elif name not in new:
            lines.append(f"- {name}")
        elif old[name] != new[name]:
            lines.append(f"~ {name}")
    return lines


def _input_digests(summary: Mapping[str, Any]) -> dict[str, Any]:
    return {name: item.get("digest") for name, item in summary.items()}


def _version_summary(part: Mapping[str, Any]) -> str:
    """Display form of the ``version`` segment: ``version=`` plus ``produces=`` when set."""

    summary = repr(part.get("summary"))
    produces = part.get("produces")
    return summary if produces is None else f"version={summary}, produces={produces!r}"


def segment_diffs(
    old: Mapping[str, Any] | None, new: Mapping[str, Any] | None
) -> list[SegmentDiff]:
    if old is None or new is None:
        return []
    diffs = []
    for segment in SEGMENTS:
        before = old.get(segment, {})
        after = new.get(segment, {})
        changed = before.get("hash") != after.get("hash")
        details: list[str] = []
        if changed:
            old_summary, new_summary = before.get("summary"), after.get("summary")
            if segment == "inputs":
                details = _diff_mapping(
                    _input_digests(old_summary or {}), _input_digests(new_summary or {})
                )
            elif segment == "version":
                details = [f"{_version_summary(before)} -> {_version_summary(after)}"]
            elif isinstance(old_summary, Mapping) and isinstance(new_summary, Mapping):
                details = _diff_mapping(old_summary, new_summary)
            else:
                details = [f"{old_summary!r} -> {new_summary!r}"]
        diffs.append(SegmentDiff(segment, changed, tuple(details)))
    return diffs


def source_diff(recorded: Mapping[str, Any] | None, current: Mapping[str, Any]) -> SourceDiff:
    now_symbols = {
        **current.get("symbols", {}),
        **{f"ext:{k}": v for k, v in current.get("external", {}).items()},
    }
    if recorded is None:
        before: dict[str, Any] = {}
    else:
        before = {
            **recorded.get("symbols", {}),
            **{f"ext:{k}": v for k, v in recorded.get("external", {}).items()},
        }
    changed = tuple(sorted(k for k in now_symbols if k in before and before[k] != now_symbols[k]))
    added = tuple(sorted(k for k in now_symbols if k not in before)) if recorded else ()
    removed = tuple(sorted(k for k in before if k not in now_symbols))
    return SourceDiff(
        mode=current.get("mode", "closure"),
        fallback_reason=current.get("fallback_reason"),
        changed=changed,
        added=added,
        removed=removed,
        symbols=tuple(current.get("symbols", {})),
        external=tuple(current.get("external", {})),
    )


def inspect_unit(session: Session, plan: StagePlan, unit: UnitPlan) -> InspectView:
    node = plan.node
    row = (
        session.repo.get_unit(session.branch_id, node.name, unit.unit_key)
        if session.repo is not None
        else None
    )
    current = None
    if unit.upstream_keys is not None:
        current = compute_input_key(
            config=session.config_dump,
            config_access=row.config_access if row is not None else None,
            upstream=unit.upstream_keys,
            inputs=unit.inputs(),
            values=unit.values(),
            version=node.spec.version,
            produces=node.spec.produces_key,
        ).components
    unit_dir = session.unit_dir(node.name, node.storage, unit.unit.relpath)
    artifact = None
    log_path = None
    if row is not None:
        if row.artifact_relpath and row.storage_name in session.storage_roots:
            artifact = session.storage_roots[row.storage_name] / row.artifact_relpath
        if row.last_run_id:
            candidate = layout.unit_log_path(session.root, row.last_run_id, node.name, row.unit_key)
            log_path = candidate if candidate.exists() else None
    return InspectView(
        stage=node.name,
        unit_key=unit.unit_key,
        decision=unit.decision,
        reason=unit.reason,
        reasons=unit.reasons,
        labels=unit.labels,
        row=row,
        segments=tuple(segment_diffs(row.key_components if row else None, current)),
        source=source_diff(row.source_detail if row else None, unit.cell.source.detail),
        unit_dir=unit_dir,
        artifact_path=artifact,
        log_path=log_path,
        current_components=current,
    )
