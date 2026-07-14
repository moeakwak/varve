"""Rich rendering for status-aware logical Stage topology."""

from __future__ import annotations

from collections.abc import Hashable
from typing import Any

from netext import ArrowTip, ConsoleGraph, EdgeRoutingMode, EdgeSegmentDrawingMode
from netext.layout_engines import LayoutDirection, SugiyamaLayout
from rich import box as rich_box
from rich.console import Console
from rich.style import Style
from rich.text import Text

from varve.status import PipelineStatus, StageStatusGroup
from varve.style import STATUS_STYLES

STAGE_NAME_LIMIT = 32


def wrap_stage_name(name: str, *, limit: int = STAGE_NAME_LIMIT) -> tuple[str, ...]:
    """Split a Stage name into at most two display lines under a fixed width."""

    if len(name) <= limit:
        return (name,)
    split_at = _preferred_split(name, limit)
    if split_at is None:
        return (name[:limit], _middle_truncate(name[limit:], limit))
    first = name[:split_at]
    second = name[split_at:]
    if len(first) > limit:
        first = first[:limit]
    if len(second) <= limit:
        return (first, second)
    return (first, _middle_truncate(second, limit))


def _preferred_split(name: str, limit: int) -> int | None:
    # Prefer the last separator that keeps the first line within the limit.
    best: int | None = None
    for index, char in enumerate(name):
        if char in {"_", "-"} and 0 < index + 1 <= limit:
            best = index + 1
        if index + 1 >= limit and best is not None:
            break
    return best


def _middle_truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return "…"
    keep = limit - 1
    head = (keep + 1) // 2
    tail = keep - head
    return f"{value[:head]}…{value[-tail:] if tail else ''}"


def format_group_progress(group: StageStatusGroup) -> Text:
    """Render status/progress text for one logical Stage node."""

    if group.is_matrix:
        if group.hit_cells == group.cell_count and group.cell_count > 0:
            return Text("✓", style=STATUS_STYLES["hit"])
        progress = Text(
            f"{group.hit_cells}/{group.cell_count} cells",
            style=STATUS_STYLES["needs-run"],
        )
        if group.status in {"needs-review", "failed", "error"}:
            progress.append(" · ", style="dim")
            progress.append_text(_aggregate_status_label(group.status))
        return progress
    if group.status == "hit":
        return Text("✓", style=STATUS_STYLES["hit"])
    if group.batch_completed is not None and group.batch_total is not None:
        progress = Text(
            f"{group.batch_completed}/{group.batch_total} batches",
            style=STATUS_STYLES["needs-run"],
        )
        if group.status in {"needs-review", "failed", "error"}:
            progress.append(" · ", style="dim")
            progress.append_text(_aggregate_status_label(group.status))
        return progress
    if group.status == "needs-review":
        return Text("! needs-review", style=STATUS_STYLES[group.status])
    if group.status == "failed":
        return Text("✕ failed", style=STATUS_STYLES[group.status])
    if group.status == "error":
        return Text("! error", style=STATUS_STYLES[group.status])
    if group.status == "resume":
        return Text("resume", style=STATUS_STYLES[group.status])
    return Text("pending", style=STATUS_STYLES["needs-run"])


def _aggregate_status_label(status: str) -> Text:
    if status == "needs-review":
        return Text("! needs-review", style=STATUS_STYLES[status])
    if status == "failed":
        return Text("✕ failed", style=STATUS_STYLES[status])
    if status == "error":
        return Text("! error", style=STATUS_STYLES[status])
    return Text(status, style=STATUS_STYLES.get(status, ""))


def node_label(group: StageStatusGroup) -> Text:
    label = Text()
    for index, line in enumerate(wrap_stage_name(group.base_name)):
        if index:
            label.append("\n")
        label.append(line, style="bold white")
    label.append("  " if group.status == "hit" else "\n")
    label.append_text(format_group_progress(group))
    return label


def _content_renderer(
    _node_str: str,
    data: dict[str, Any],
    _content_style: Style,
) -> Text:
    return data["$label"]


def build_plan_graph(status: PipelineStatus, *, console: Console) -> ConsoleGraph:
    groups = status.groups
    selected_bases = {group.base_name for group in groups}
    nodes: dict[Hashable, dict[str, Any]] = {
        group.base_name: {
            "$label": node_label(group),
            "$content-renderer": _content_renderer,
            "$shape": "box",
            "$box-type": rich_box.ROUNDED,
            "$padding": (0, 1),
        }
        for group in groups
    }
    edge_attrs: dict[str, Any] = {
        "$edge-routing-mode": EdgeRoutingMode.ORTHOGONAL,
        "$edge-segment-drawing-mode": EdgeSegmentDrawingMode.BOX,
        "$end-arrow-tip": ArrowTip.ARROW,
        "$style": Style(dim=True),
    }
    edges: list[tuple[Hashable, Hashable, dict[str, Any]]] = []
    for group in groups:
        for need in group.logical_needs:
            if need in selected_bases:
                edges.append((need, group.base_name, edge_attrs))
    return ConsoleGraph(
        nodes,
        edges,
        console=console,
        layout_engine=SugiyamaLayout(LayoutDirection.TOP_DOWN),
    )


def render_plan(
    console: Console,
    status: PipelineStatus,
    *,
    target_module: str | None = None,
) -> None:
    heading = Text("Plan", style="bold")
    heading.append(" · ", style="dim")
    heading.append(status.branch, style="dim")
    if target_module is not None:
        heading.append(" · ", style="dim")
        heading.append(target_module, style="blue")
    console.print(heading)
    console.print()
    if not status.groups:
        console.print(Text("No selected stages.", style="dim"))
        return
    console.print(build_plan_graph(status, console=console))
