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
    # Prefer the last separator that keeps the first line within the limit.
    split_at = max(name.rfind("_", 0, limit), name.rfind("-", 0, limit)) + 1 or None
    if split_at is None:
        return (name[:limit], _middle_truncate(name[limit:], limit))
    first = name[:split_at]
    second = name[split_at:]
    if len(second) <= limit:
        return (first, second)
    return (first, _middle_truncate(second, limit))


def _middle_truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    if limit <= 1:
        return "…"
    head = limit // 2
    tail = limit - head - 1
    return f"{value[:head]}…{value[-tail:] if tail else ''}"


def format_group_progress(group: StageStatusGroup) -> Text:
    """Render status/progress text for one logical Stage node."""

    if group.is_matrix:
        total = len(group.cells)
        hit = sum(cell.status == "hit" for cell in group.cells)
        if hit == total and total > 0:
            return Text("✓", style=STATUS_STYLES["hit"])
        return _progress(f"{hit}/{total} cells", group.status)
    if group.status == "hit":
        return Text("✓", style=STATUS_STYLES["hit"])
    cell = group.cells[0]
    if cell.batch_progress is not None:
        completed, total = cell.batch_progress
        return _progress(f"{completed}/{total} batches", group.status)
    if group.status in {"needs-review", "failed", "error"}:
        return _aggregate_status_label(group.status)
    if group.status == "resume":
        return Text("resume", style=STATUS_STYLES[group.status])
    return Text("pending", style=STATUS_STYLES["needs-run"])


def _aggregate_status_label(status: str) -> Text:
    marker = {"needs-review": "! ", "failed": "✕ ", "error": "! "}.get(status, "")
    return Text(marker + status, style=STATUS_STYLES.get(status, ""))


def _progress(label: str, status: str) -> Text:
    result = Text(label, style=STATUS_STYLES["needs-run"])
    if status in {"needs-review", "failed", "error"}:
        result.append(" · ", style="dim")
        result.append_text(_aggregate_status_label(status))
    return result


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
        for need in group.cells[0].logical_needs:
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
