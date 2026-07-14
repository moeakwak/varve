"""Rich rendering for structured pipeline status."""

from __future__ import annotations

import re
from typing import Literal

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from varve.status import PipelineStatus, StageReviewStatus, StageStatus, StageStatusGroup
from varve.style import format_elapsed, status_text

_REASON_KEYWORD_STYLES = {
    "changed": "yellow",
    "added": "green",
    "removed": "red",
    "missing": "yellow",
    "hit": "green",
    "resume": "yellow",
    "no cache": "yellow",
    "forced": "yellow",
}
_SUMMARY_HEADERS = ("STAGE", "STATUS", "REVIEW", "CELLS", "DURATION", "NEEDS", "REASON")


def format_needs(needs: tuple[str, ...]) -> str:
    if not needs:
        return "-"
    visible = ", ".join(needs[:2])
    hidden = len(needs) - 2
    return f"{visible} · +{hidden} more" if hidden > 0 else visible


def reason_text(reason: str) -> Text:
    upstream = re.match(r"upstream '([^']+)'", reason)
    if upstream is None:
        label = Text(reason)
    else:
        label = Text("upstream ")
        label.append(upstream.group(1), style="bold")
        label.append(reason[upstream.end() :])
    for keyword, style in _REASON_KEYWORD_STYLES.items():
        for match in re.finditer(rf"\b{re.escape(keyword)}\b", label.plain):
            label.stylize(style, match.start(), match.end())
    return label


def _render_pipeline_heading(
    console: Console,
    status: PipelineStatus,
    *,
    target_module: str | None = None,
) -> None:
    title = "Pipeline status"
    if target_module is not None:
        title += f"  {target_module} · {status.pipeline}"
    console.print(
        Text.assemble(
            (title, "varve.dependency.stage"),
            (f"  branch {status.branch} · output {status.output_root}", "dim"),
        )
    )
    console.print()


def format_group_cells(group: StageStatusGroup) -> Text:
    total = len(group.cells)
    if len(group.status_counts) == 1:
        status, count = group.status_counts[0]
        result = Text(f"{count}/{total} ")
        token = status_text(status)
        result.append(token.plain, style=token.style)
        return result
    result = Text()
    for index, (status, count) in enumerate(group.status_counts):
        if index:
            result.append(" · ", style="dim")
        result.append(f"{count} ")
        token = status_text(status)
        result.append(token.plain, style=token.style)
    return result


def format_group_duration(group: StageStatusGroup) -> str:
    if not group.is_matrix:
        return format_elapsed(group.duration)
    recorded = group.recorded_duration_count
    total = len(group.cells)
    duration = format_elapsed(group.duration, missing="-")
    return duration if recorded == total else f"{duration} · {recorded}/{total}"


def review_text(review: StageReviewStatus) -> Text:
    if review.relationship != "changed":
        return Text("-")
    if review.decision == "none":
        return Text("required", style="yellow")
    if review.decision == "reuse":
        return Text("reuse", style="green")
    return Text("invalidate", style="yellow")


def _group_values(group: StageStatusGroup) -> tuple[str | Text, ...]:
    return (
        group.base_name,
        status_text(group.status),
        review_text(group.review),
        format_group_cells(group) if group.is_matrix else "-",
        format_group_duration(group),
        format_needs(group.logical_needs),
        reason_text(group.reason),
    )


def _text_len(value: str | Text) -> int:
    return len(value if isinstance(value, str) else value.plain)


def _summary_widths(groups: tuple[StageStatusGroup, ...]) -> tuple[int, ...]:
    values = tuple(_group_values(group) for group in groups)
    return tuple(
        max(len(header), *(_text_len(row[index]) for row in values))
        for index, header in enumerate(_SUMMARY_HEADERS)
    )


def _summary_table(console: Console, groups: tuple[StageStatusGroup, ...]) -> Table:
    has_matrix = any(group.is_matrix for group in groups)
    fold_core = console.width < 80
    table = Table(box=None, padding=(0, 1), header_style="bold")
    widths = _summary_widths(groups) if has_matrix else (None,) * len(_SUMMARY_HEADERS)
    headers = _SUMMARY_HEADERS if has_matrix else tuple(h for h in _SUMMARY_HEADERS if h != "CELLS")
    for header in headers:
        index = _SUMMARY_HEADERS.index(header)
        kwargs = {
            "style": "bold" if header == "STAGE" else "dim" if header == "NEEDS" else None,
            "justify": "right" if header == "DURATION" else "left",
            "no_wrap": has_matrix
            or (header in {"STAGE", "STATUS", "REVIEW", "DURATION"} and not fold_core),
            "overflow": (
                "ellipsis"
                if has_matrix
                else "ignore"
                if header in {"STAGE", "STATUS", "REVIEW", "DURATION"} and not fold_core
                else "fold"
            ),
        }
        if has_matrix and header in {"STAGE", "STATUS", "REVIEW", "CELLS", "DURATION"}:
            kwargs.update(width=widths[index], min_width=widths[index])
        elif has_matrix:
            kwargs["min_width"] = len(header)
        table.add_column(header, **kwargs)
    for group in groups:
        values = _group_values(group)
        table.add_row(*(values if has_matrix else values[:3] + values[4:]))
    return table


def _fit_text(value: str | Text, width: int, *, style: str | None = None) -> Text:
    if isinstance(value, str):
        result = Text(value) if style is None else Text(value, style=style)
    else:
        result = value.copy()
    if result.style:
        result.stylize(result.style, 0, len(result))
    result.truncate(width, overflow="ellipsis", pad=True)
    return result


def _compact_row(parts: tuple[tuple[str | Text, int, str | None], ...]) -> Text:
    row = Text()
    for index, (value, width, style) in enumerate(parts):
        if index:
            row.append("  ")
        row.append_text(_fit_text(value, width, style=style))
    return row


def _compact_matrix_summary(console: Console, groups: tuple[StageStatusGroup, ...]) -> None:
    stage_width, status_width, review_width, cells_width, duration_width, _, _ = _summary_widths(
        groups
    )
    cells_width = min(20, cells_width)
    fixed_width = stage_width + status_width + review_width + cells_width + duration_width + 12
    flexible_width = max(console.width - fixed_width, 10)
    needs_width = max(5, flexible_width * 3 // 5)
    reason_width = max(5, flexible_width - needs_width)
    widths = (
        stage_width,
        status_width,
        review_width,
        cells_width,
        duration_width,
        needs_width,
        reason_width,
    )
    console.print(
        _compact_row(
            tuple((header, width, "bold") for header, width in zip(_SUMMARY_HEADERS, widths))
        )
    )
    for group in groups:
        console.print(
            _compact_row(
                tuple(
                    zip(
                        _group_values(group),
                        widths,
                        ("bold", None, None, None, None, "dim", None),
                        strict=True,
                    )
                )
            )
        )


def render_pipeline_summary(
    console: Console,
    status: PipelineStatus,
    *,
    target_module: str | None = None,
) -> None:
    _render_pipeline_heading(console, status, target_module=target_module)
    if (
        status.selector is not None
        and not status.selector.is_concrete
        and any(group.is_matrix for group in status.groups)
    ):
        selector_heading = Text(status.selector.canonical, style="varve.dependency.stage")
        selector_heading.append(
            f"  {status.selector.matched_count} cells",
            style="dim",
        )
        console.print(selector_heading)
        console.print()
    if any(group.is_matrix for group in status.groups) and console.width < 120:
        _compact_matrix_summary(console, status.groups)
    else:
        console.print(_summary_table(console, status.groups))
    console.print()
    hints = []
    if any(group.is_matrix for group in status.groups):
        hints.append("add --expand to show matrix cells")
    if any(not group.is_matrix for group in status.groups):
        hints.append("select a stage and add --expand for details")
    if hints:
        console.print(Text("Status is folded; " + "; ".join(hints) + ".", style="dim"))


def render_expanded_groups(
    console: Console,
    status: PipelineStatus,
    *,
    target_module: str | None = None,
) -> None:
    _render_pipeline_heading(console, status, target_module=target_module)
    ordinary = tuple(group for group in status.groups if not group.is_matrix)
    for group in status.groups:
        if not group.is_matrix:
            continue
        target = status.selector.canonical if status.selector is not None else group.base_name
        heading = Text(target, style="varve.dependency.stage")
        heading.append(
            f"  {len(group.cells)} cells · needs {format_needs(group.logical_needs)}",
            style="dim",
        )
        if group.review.relationship == "changed":
            heading.append(" · review ", style="dim")
            heading.append_text(review_text(group.review))
        console.print(heading)
        table = Table(box=None, padding=(0, 1), header_style="bold")
        for axis in group.axes:
            table.add_column(axis.upper(), style="bold")
        table.add_column("STATUS")
        table.add_column("DURATION", justify="right")
        table.add_column("REASON", overflow="fold")
        for cell in group.cells:
            table.add_row(
                *(coordinate.value_id for coordinate in cell.cell),
                status_text(cell.status),
                format_elapsed(cell.duration, missing="-"),
                reason_text(cell.summary_reason),
            )
        console.print(table)
        console.print()
    if ordinary:
        console.print(Text("Ordinary stages", style="bold"))
        console.print(_summary_table(console, ordinary))
        console.print()
    console.print(
        "[dim]Select a concrete cell or ordinary stage and add[/] [bold]--expand[/] [dim]for details.[/]"
    )


def key_inputs_table(stage: StageStatus) -> Table:
    assert stage.key_inputs is not None
    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    table.add_column()
    values = {
        "Config": ", ".join(f"{name}={value!r}" for name, value in stage.key_inputs.config.items()),
        "Inputs": ", ".join(stage.key_inputs.inputs),
        "Values": ", ".join(f"{name}={value!r}" for name, value in stage.key_inputs.values.items()),
        "Upstream": ", ".join(stage.key_inputs.upstreams),
    }
    for label, value in values.items():
        table.add_row(label, value or "-")
    return table


def render_stage_status(
    console: Console,
    stage: StageStatus,
    stage_review: StageReviewStatus,
) -> None:
    heading = Text(stage.name, style="varve.dependency.stage")
    heading.append("  ")
    heading.append_text(status_text(stage.status))
    console.print(heading)
    console.print()

    overview = Table(box=None, show_header=False, padding=(0, 2))
    overview.add_column(style="dim", no_wrap=True)
    overview.add_column(overflow="fold")
    detail_reason = stage.reason
    if stage.source_relationship == "changed" and stage.execution_reason not in {
        "hit",
        stage.reason,
    }:
        detail_reason += f" · {stage.execution_reason}"
    overview.add_row("Reason", reason_text(detail_reason))
    if stage.failure is not None:
        overview.add_row("Failure", stage.failure)
    source_label, source_style = {
        "not-applicable": ("not recorded", "dim"),
        "current": ("current", "green"),
        "changed": ("changed", "yellow"),
    }[stage.source_relationship]
    source = Text(source_label, style=source_style)
    overview.add_row("Source", source)
    if stage_review.relationship == "changed":
        overview.add_row("Stage review", review_text(stage_review))
    overview.add_row("Needs", ", ".join(stage.needs) if stage.needs else "-")
    overview.add_row("Decision key", stage.decision_key or "unavailable")
    overview.add_row("Stored key", stage.stored_key or "-")

    content: list[RenderableType] = [overview]
    if stage.key_inputs is None:
        content.extend(
            [Text(), Text(f"Key inputs unavailable: {stage.unavailable_reason}", style="dim")]
        )
    else:
        content.extend([Text(), Text("Key inputs", style="bold"), key_inputs_table(stage)])
    console.print(Panel(Group(*content), border_style="dim"))

    if stage.source_changes:
        changes = Table(title="Changed source files", box=None)
        changes.add_column("PATH")
        changes.add_column("CHANGE")
        for path, change in sorted(stage.source_changes.items()):
            changes.add_row(path, change)
        console.print(changes)


def render_status(
    console: Console,
    status: PipelineStatus,
    *,
    view: Literal["summary", "cells", "detail"],
    dependency_depth: int | None = 0,
    target_module: str | None = None,
) -> None:
    if view == "summary":
        render_pipeline_summary(console, status, target_module=target_module)
        return
    if view == "cells":
        render_expanded_groups(console, status, target_module=target_module)
        return
    if target_module is not None:
        _render_pipeline_heading(console, status, target_module=target_module)
    reviews = {group.base_name: group.review for group in status.groups}
    for index, stage in enumerate(status.stages):
        if index:
            console.print()
        render_stage_status(console, stage, reviews[stage.base_name])


def status_view(status: PipelineStatus, *, expand: bool) -> Literal["summary", "cells", "detail"]:
    """Choose one shared view from selector metadata and display intent."""

    has_matrix = any(group.is_matrix for group in status.groups)
    if status.selector is None:
        if expand and has_matrix:
            return "cells"
        return "detail" if expand else "summary"
    if status.selector.is_concrete or not has_matrix:
        return "detail"
    return "cells" if expand else "summary"
