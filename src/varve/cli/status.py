"""Rich rendering for structured pipeline status."""

from __future__ import annotations

import re
from typing import Literal

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from varve.status import PipelineStatus, StageStatus, StageStatusGroup
from varve.style import format_elapsed, status_text

_COMPACT_REASON_PREFIXES = (
    ("global referenced by ", "global reference"),
    ("closure referenced by ", "closure reference"),
    ("default value declared by ", "default value"),
    ("module attribute referenced by ", "module attribute"),
    ("base class of ", "base class"),
)
_REMOVED_PREVIEW_LIMIT = 5
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


def format_needs(needs: tuple[str, ...]) -> str:
    if not needs:
        return "-"
    visible = ", ".join(needs[:2])
    hidden = len(needs) - 2
    return f"{visible} · +{hidden} more" if hidden > 0 else visible


def compact_reason(reason: str) -> str:
    for prefix, compact in _COMPACT_REASON_PREFIXES:
        if reason.startswith(prefix):
            return compact
    return reason


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


def _summary_table(console: Console, groups: tuple[StageStatusGroup, ...]) -> Table:
    has_matrix = any(group.is_matrix for group in groups)
    fold_core = console.width < 80
    core_overflow = "fold" if fold_core else "ignore"
    table = Table(box=None, padding=(0, 1), header_style="bold")
    stage_width = max((len(group.base_name) for group in groups), default=5) if has_matrix else None
    stage_width = max(stage_width or 0, len("STAGE")) if has_matrix else None
    table.add_column(
        "STAGE",
        style="bold",
        width=stage_width,
        min_width=stage_width,
        no_wrap=has_matrix or not fold_core,
        overflow="ellipsis" if has_matrix else core_overflow,
    )
    status_width = max((len(group.status) for group in groups), default=6) if has_matrix else None
    status_width = max(status_width or 0, len("STATUS")) if has_matrix else None
    table.add_column(
        "STATUS",
        width=status_width,
        min_width=status_width,
        no_wrap=has_matrix or not fold_core,
        overflow="ellipsis" if has_matrix else core_overflow,
    )
    if has_matrix:
        cells_width = max(
            len(format_group_cells(group).plain) if group.is_matrix else 1 for group in groups
        )
        table.add_column(
            "CELLS",
            width=max(cells_width, len("CELLS")),
            min_width=max(cells_width, len("CELLS")),
            no_wrap=True,
            overflow="ellipsis",
        )
    duration_width = (
        max(len(format_group_duration(group)) for group in groups) if has_matrix else None
    )
    duration_width = max(duration_width or 0, len("DURATION")) if has_matrix else None
    table.add_column(
        "DURATION",
        justify="right",
        width=duration_width,
        min_width=duration_width,
        no_wrap=has_matrix or not fold_core,
        overflow="ellipsis" if has_matrix else core_overflow,
    )
    table.add_column(
        "NEEDS",
        style="dim",
        min_width=5 if has_matrix else None,
        no_wrap=has_matrix,
        overflow="ellipsis" if has_matrix else "fold",
    )
    table.add_column(
        "REASON",
        min_width=6 if has_matrix else None,
        no_wrap=has_matrix,
        overflow="ellipsis" if has_matrix else "fold",
    )
    for group in groups:
        row: list[RenderableType] = [
            group.base_name,
            status_text(group.status),
        ]
        if has_matrix:
            row.append(format_group_cells(group) if group.is_matrix else "-")
        row.extend(
            [
                format_group_duration(group),
                format_needs(group.logical_needs),
            ]
        )
        row.append(reason_text(group.reason))
        table.add_row(*row)
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
    stage_width = max(len("STAGE"), *(len(group.base_name) for group in groups))
    status_width = max(len("STATUS"), *(len(group.status) for group in groups))
    cells_width = min(
        20,
        max(
            len("CELLS"),
            *(len(format_group_cells(group).plain) if group.is_matrix else 1 for group in groups),
        ),
    )
    duration_width = max(len("DURATION"), *(len(format_group_duration(group)) for group in groups))
    fixed_width = stage_width + status_width + cells_width + duration_width + 10
    flexible_width = max(console.width - fixed_width, 10)
    needs_width = max(5, flexible_width * 3 // 5)
    reason_width = max(5, flexible_width - needs_width)
    console.print(
        _compact_row(
            (
                ("STAGE", stage_width, "bold"),
                ("STATUS", status_width, "bold"),
                ("CELLS", cells_width, "bold"),
                ("DURATION", duration_width, "bold"),
                ("NEEDS", needs_width, "bold"),
                ("REASON", reason_width, "bold"),
            )
        )
    )
    for group in groups:
        console.print(
            _compact_row(
                (
                    (group.base_name, stage_width, "bold"),
                    (status_text(group.status), status_width, None),
                    (
                        format_group_cells(group) if group.is_matrix else "-",
                        cells_width,
                        None,
                    ),
                    (format_group_duration(group), duration_width, None),
                    (format_needs(group.logical_needs), needs_width, "dim"),
                    (reason_text(group.reason), reason_width, None),
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
    table.add_row(
        "Config",
        ", ".join(f"{name}={value!r}" for name, value in stage.key_inputs.config.items()) or "-",
    )
    table.add_row("Inputs", ", ".join(stage.key_inputs.inputs) or "-")
    table.add_row(
        "Values",
        ", ".join(f"{name}={value!r}" for name, value in stage.key_inputs.values.items()) or "-",
    )
    table.add_row("Upstream", ", ".join(stage.key_inputs.upstreams) or "-")
    return table


def render_stage_status(
    console: Console,
    stage: StageStatus,
    *,
    depth: int | None,
    show_keys: bool,
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
    source = Text()
    if stage.source_relationship == "not-applicable":
        source.append("not recorded", style="dim")
    elif stage.source_relationship == "current":
        source.append("current", style="green")
    else:
        source.append("changed", style="yellow")
    overview.add_row("Source", source)
    if stage.source_relationship == "changed":
        review = {
            "none": Text("required", style="yellow"),
            "accept": Text("accepted", style="green"),
            "reject": Text("rejected", style="yellow"),
        }[stage.review_decision]
        overview.add_row("Review", review)
    overview.add_row("Needs", ", ".join(stage.needs) if stage.needs else "-")
    if show_keys:
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
    for index, stage in enumerate(status.stages):
        if index:
            console.print()
        render_stage_status(
            console,
            stage,
            depth=dependency_depth,
            show_keys=True,
        )


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
