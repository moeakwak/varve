"""Matrix-aware display policy and aggregation for pipeline runs."""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal

from varve.engine.state import EXECUTION_STATUS_SEVERITY, EffectiveStatus, ExecutionStatus
from varve.matrix import PipelineGraph
from varve.store.store import Store

RunDisplayMode = Literal["auto", "expand", "compact"]

# Auto mode folds only genuinely high-cardinality selected groups. A known slow
# cell stays visible because its lifecycle is useful even in a large matrix.
AUTO_COMPACT_MIN_CELLS = 8
AUTO_EXPAND_SLOW_SECONDS = 30.0


@dataclass(frozen=True)
class StageOutcome:
    stage: str
    status: ExecutionStatus
    reason: str
    elapsed: float | None
    display_base: str | None = None
    display_compact: bool = False
    display_cells: int = 1


@dataclass(frozen=True)
class RunDisplayGroup:
    base_name: str
    stages: tuple[str, ...]
    compact: bool


@dataclass(frozen=True)
class RunDisplayPlan:
    groups: tuple[RunDisplayGroup, ...]
    by_stage: Mapping[str, RunDisplayGroup]

    def plan_entries(self) -> tuple[str, ...]:
        """Legacy lifecycle/outcome helper; always folds Matrix groups."""

        return tuple(
            f"{group.base_name} ({len(group.stages)} cells)"
            if len(group.stages) > 1 or group.compact
            else group.stages[0]
            if group.stages
            else group.base_name
            for group in self.groups
        )

    def outcome(
        self,
        stage: str,
        status: ExecutionStatus,
        reason: str,
        elapsed: float | None,
    ) -> StageOutcome:
        group = self.by_stage[stage]
        return StageOutcome(
            stage=stage,
            status=status,
            reason=reason,
            elapsed=elapsed,
            display_base=group.base_name,
            display_compact=group.compact,
            display_cells=len(group.stages),
        )


def build_run_display_plan(
    graph: PipelineGraph,
    selected: set[str],
    store: Store,
    *,
    mode: RunDisplayMode,
) -> RunDisplayPlan:
    """Choose one stable display mode for every selected base-stage group."""

    if mode not in {"auto", "expand", "compact"}:
        raise ValueError(f"Invalid run display mode: {mode!r}")

    ordered = [stage for stage in graph.topo_order() if stage in selected]
    grouped: dict[str, list[str]] = {}
    for stage_name in ordered:
        spec = graph.stages[stage_name]
        base_name = spec.base_name or spec.name
        grouped.setdefault(base_name, []).append(stage_name)

    groups: list[RunDisplayGroup] = []
    by_stage: dict[str, RunDisplayGroup] = {}
    for base_name, stages in grouped.items():
        matrix_group = bool(graph.stages[stages[0]].cell)
        if not matrix_group or mode == "expand":
            compact = False
        elif mode == "compact":
            compact = True
        else:
            has_slow_history = any(
                (record := store.read_success(stage)) is not None
                and record.elapsed is not None
                and record.elapsed >= AUTO_EXPAND_SLOW_SECONDS
                for stage in stages
            )
            compact = len(stages) >= AUTO_COMPACT_MIN_CELLS and not has_slow_history
        group = RunDisplayGroup(base_name, tuple(stages), compact)
        groups.append(group)
        by_stage.update((stage, group) for stage in stages)
    return RunDisplayPlan(tuple(groups), MappingProxyType(by_stage))


def _status_counts(
    outcomes: list[StageOutcome],
) -> tuple[tuple[ExecutionStatus, int], ...]:
    counts = Counter(outcome.status for outcome in outcomes)
    return tuple((status, counts[status]) for status in EXECUTION_STATUS_SEVERITY if counts[status])


def _status_distribution(outcomes: list[StageOutcome]) -> str:
    return ", ".join(f"{count} {status}" for status, count in _status_counts(outcomes))


def format_run_order_marker(
    *,
    base_name: str,
    stages: tuple[str, ...],
    is_matrix: bool,
    forced: bool,
    status_by_stage: Mapping[str, EffectiveStatus],
    batch_completed: int | None = None,
    batch_total: int | None = None,
) -> str:
    """Build one base-stage token for the run-order summary line."""

    if forced:
        return f"{base_name} run"
    if is_matrix:
        hit = sum(status_by_stage.get(stage) == "hit" for stage in stages)
        total = len(stages)
        if hit == total and total > 0:
            return f"{base_name} ✓"
        return f"{base_name} {hit}/{total}"
    status = status_by_stage.get(stages[0], "needs-run")
    if status == "hit":
        return f"{base_name} ✓"
    if batch_completed is not None and batch_total is not None:
        progress = f"{base_name} {batch_completed}/{batch_total}"
        if status == "needs-review":
            return f"{progress} · ! needs-review"
        if status == "failed":
            return f"{progress} · ✕ failed"
        if status == "error":
            return f"{progress} · ! error"
        return progress
    if status == "needs-review":
        return f"{base_name} ! needs-review"
    if status == "failed":
        return f"{base_name} ✕ failed"
    if status == "error":
        return f"{base_name} ! error"
    if status == "resume":
        return f"{base_name} resume"
    return f"{base_name} run"


class RunReporter:
    """Emit bounded live logs while retaining concrete debug diagnostics."""

    def __init__(self, plan: RunDisplayPlan, logger: logging.Logger) -> None:
        self.plan = plan
        self.logger = logger
        self._started: set[str] = set()
        self._completed: dict[str, list[StageOutcome]] = {}
        self.active_stage: str | None = None

    def log_plan(
        self,
        *,
        markers: tuple[str, ...] | None = None,
    ) -> None:
        entries = markers if markers is not None else self.plan.plan_entries()
        self.logger.info("Run order: %s", " → ".join(entries))

    def start(self, stage: str) -> None:
        self.active_stage = stage
        group = self.plan.by_stage[stage]
        if not group.compact or group.base_name in self._started:
            return
        self._started.add(group.base_name)
        self.logger.info("[%s] start · %d cells", group.base_name, len(group.stages))

    def lifecycle(self, stage: str, status: str, reason: str | None = None) -> None:
        group = self.plan.by_stage[stage]
        suffix = f" · {reason}" if reason is not None and reason != status else ""
        level = logging.DEBUG if group.compact else logging.INFO
        self.logger.log(level, "[%s] %s%s", stage, status, suffix)

    def input_key(self, stage: str, value: str) -> None:
        self.logger.debug("[%s] input_key %s", stage, value)

    def failure_current(self, error: BaseException) -> None:
        if self.active_stage is not None:
            # Failures are always concrete, even for compact groups.
            self.logger.error("[%s] error · %s", self.active_stage, error)

    def record(self, outcome: StageOutcome) -> None:
        group = self.plan.by_stage[outcome.stage]
        if not group.compact:
            return
        completed = self._completed.setdefault(group.base_name, [])
        completed.append(outcome)
        if outcome.elapsed is not None and outcome.elapsed >= AUTO_EXPAND_SLOW_SECONDS:
            self.logger.info("[%s] slow · %.2fs", outcome.stage, outcome.elapsed)
        if len(completed) != len(group.stages):
            return
        ran = sum(item.elapsed is not None for item in completed)
        elapsed = sum(item.elapsed or 0.0 for item in completed)
        self.logger.info(
            "[%s] done · %d cells · %s · ran %d · %.2fs",
            group.base_name,
            len(group.stages),
            _status_distribution(completed),
            ran,
            elapsed,
        )


@dataclass(frozen=True)
class RunOutcomeRow:
    stage: str
    status: str
    status_counts: tuple[tuple[ExecutionStatus, int], ...]
    reason: str
    cells: int
    ran: int
    elapsed: float | None
    grouped: bool


def outcome_rows(outcomes: list[StageOutcome]) -> tuple[RunOutcomeRow, ...]:
    """Fold outcomes using the display decision captured before execution."""

    compact = {
        base: [item for item in outcomes if item.display_base == base]
        for base in dict.fromkeys(item.display_base for item in outcomes if item.display_compact)
    }
    rows: list[RunOutcomeRow] = []
    for outcome in outcomes:
        if not outcome.display_compact:
            rows.append(
                RunOutcomeRow(
                    stage=outcome.stage,
                    status=outcome.status,
                    status_counts=((outcome.status, 1),),
                    reason=outcome.reason,
                    cells=1,
                    ran=int(outcome.elapsed is not None),
                    elapsed=outcome.elapsed,
                    grouped=False,
                )
            )
            continue
        assert outcome.display_base is not None
        group = compact.pop(outcome.display_base, None)
        if group is None:
            continue
        ran = sum(item.elapsed is not None for item in group)
        rows.append(
            RunOutcomeRow(
                stage=outcome.display_base,
                status=_status_distribution(group),
                status_counts=_status_counts(group),
                reason="-",
                cells=outcome.display_cells,
                ran=ran,
                elapsed=sum(item.elapsed or 0.0 for item in group) if ran else None,
                grouped=True,
            )
        )
    return tuple(rows)
