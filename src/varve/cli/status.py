"""Rich rendering for structured pipeline status."""

from __future__ import annotations

import re
from typing import Literal

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from varve.keying.dependencies import DependencyNode, SourceDependencies
from varve.status import PipelineStatus, SourceChange, StageStatus, StageStatusGroup
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
    "dirty": "red",
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


def relative_qualified_name(
    graph: SourceDependencies,
    *,
    parent: str,
    child: str,
) -> str:
    child_name = graph.nodes[child].qualified_name
    if parent == "stage":
        return child_name
    parent_parts = graph.nodes[parent].qualified_name.split(".")
    child_parts = child_name.split(".")
    common = 0
    for parent_part, child_part in zip(parent_parts, child_parts):
        if parent_part != child_part:
            break
        common += 1
    if common == 0 or common == len(child_parts):
        return child_name
    return ".".join(child_parts[common:])


def _render_pipeline_heading(console: Console, status: PipelineStatus) -> None:
    console.print(
        Text.assemble(
            ("Pipeline status", "varve.dependency.stage"),
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


def _dependency_summary(stage: StageStatus) -> Text:
    summary = Text()
    summary.append(f"{stage.direct_count} direct", style="varve.dependency.function")
    summary.append(f" · {stage.total_count} total", style="dim")
    if stage.broad_count:
        summary.append(f" · {stage.broad_count} broad", style="varve.dependency.broad")
    return summary


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
    if not has_matrix:
        table.add_column("SOURCE DEPENDENCIES", overflow="fold")
    table.add_column(
        "REASON",
        min_width=6 if has_matrix else None,
        no_wrap=has_matrix,
        overflow="ellipsis" if has_matrix else "fold",
    )
    for group in groups:
        stage = group.cells[0]
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
        if not has_matrix:
            row.append(_dependency_summary(stage))
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


def render_pipeline_summary(console: Console, status: PipelineStatus) -> None:
    _render_pipeline_heading(console, status)
    if any(group.is_matrix for group in status.groups) and console.width < 120:
        _compact_matrix_summary(console, status.groups)
    else:
        console.print(_summary_table(console, status.groups))
    console.print()
    hints = []
    if any(group.is_matrix for group in status.groups):
        hints.append("add --expand to show matrix cells")
    if any(not group.is_matrix for group in status.groups):
        hints.append("select a stage and add --expand/--all for source dependencies")
    if hints:
        console.print(Text("Status is folded; " + "; ".join(hints) + ".", style="dim"))


def render_expanded_groups(console: Console, status: PipelineStatus) -> None:
    _render_pipeline_heading(console, status)
    ordinary = tuple(group for group in status.groups if not group.is_matrix)
    for group in status.groups:
        if not group.is_matrix:
            continue
        heading = Text(group.base_name, style="varve.dependency.stage")
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
        "[dim]Select a concrete cell or ordinary stage, then add[/] [bold]--expand[/] "
        "[dim]or[/] [bold]--all[/] [dim]to inspect source dependencies;[/] "
        "[bold]--deps[/] [dim]and[/] [bold]--deps-all[/] [dim]are explicit equivalents.[/]"
    )


def child_identities(graph: SourceDependencies, parent: str) -> tuple[str, ...]:
    return tuple(sorted({edge.child for edge in graph.edges if edge.parent == parent}))


def edge_reasons(graph: SourceDependencies, parent: str, child: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            {edge.reason for edge in graph.edges if edge.parent == parent and edge.child == child}
        )
    )


def reachable_unique_identities(
    graph: SourceDependencies,
    roots: tuple[str, ...],
    *,
    exclude: set[str],
) -> set[str]:
    seen = set(exclude)
    stack = list(roots)
    reachable: set[str] = set()
    while stack:
        identity = stack.pop()
        if identity in seen:
            continue
        seen.add(identity)
        reachable.add(identity)
        stack.extend(child_identities(graph, identity))
    return reachable


def key_inputs_table(stage: StageStatus) -> Table:
    assert stage.key_inputs is not None
    table = Table(box=None, show_header=False, padding=(0, 2))
    table.add_column(style="dim", no_wrap=True)
    table.add_column()
    table.add_row(
        "Config",
        ", ".join(f"{name}={value!r}" for name, value in stage.key_inputs.config.items()) or "-",
    )
    table.add_row("Files", ", ".join(stage.key_inputs.files) or "-")
    table.add_row(
        "Values",
        ", ".join(f"{name}={value!r}" for name, value in stage.key_inputs.values.items()) or "-",
    )
    table.add_row("Upstream", ", ".join(stage.key_inputs.upstreams) or "-")
    return table


def append_change_badge(label: Text, change: SourceChange | None) -> None:
    if change is not None:
        label.append(f"  [{change}]", style=f"varve.dependency.{change}")


def dependency_label(
    node: DependencyNode,
    *,
    qualified_name: str,
    change: SourceChange | None,
) -> Text:
    label = Text()
    label.append(node.kind, style=f"varve.dependency.{node.kind}")
    label.append("  ")
    label.append(qualified_name, style="bold")
    if node.origin == "explicit":
        label.append("  [explicit]", style="varve.dependency.metadata")
    if node.scope:
        label.append(f"  [{node.scope}]", style="varve.dependency.broad")
    append_change_badge(label, change)
    return label


def removed_dependency_label(component: str) -> Text:
    parts = component.split(".", 2)
    if (
        len(parts) != 3
        or parts[0] not in {"auto", "uses"}
        or parts[1] not in {"function", "class", "module", "value"}
    ):
        explicit_name = component.removeprefix("uses.")
        label = Text(explicit_name if explicit_name != component else component, style="bold")
        if explicit_name != component:
            label.append("  [explicit]", style="varve.dependency.metadata")
    else:
        origin, kind, qualified_name = parts
        label = Text(kind, style=f"varve.dependency.{kind}")
        label.append("  ")
        label.append(qualified_name, style="bold")
        if origin == "uses":
            label.append("  [explicit]", style="varve.dependency.metadata")
    append_change_badge(label, "removed")
    return label


def append_folded_change_badges(
    label: Text,
    graph: SourceDependencies,
    identities: set[str],
    changes: dict[str, SourceChange],
) -> None:
    counts = {"changed": 0, "added": 0}
    for identity in identities:
        change = changes.get(graph.nodes[identity].component_name)
        if change in counts:
            counts[change] += 1
    for change, count in counts.items():
        if count:
            label.append(f"  [{count} {change}]", style=f"varve.dependency.{change}")


def add_dependency(
    tree: Tree,
    *,
    parent: str,
    identity: str,
    graph: SourceDependencies,
    depth: int | None,
    shown: set[str],
    changes: dict[str, SourceChange],
) -> None:
    node = graph.nodes[identity]
    change = changes.get(node.component_name)
    display_name = relative_qualified_name(
        graph,
        parent=parent,
        child=identity,
    )
    if identity in shown:
        reference_label = Text(f"↳ {display_name} already shown", style="dim")
        append_change_badge(reference_label, change)
        reference = tree.add(reference_label)
        for reason in edge_reasons(graph, parent, identity):
            reference.add(Text(compact_reason(reason), style="dim"))
        return
    shown.add(identity)
    branch = tree.add(dependency_label(node, qualified_name=display_name, change=change))
    for reason in edge_reasons(graph, parent, identity):
        branch.add(Text(compact_reason(reason), style="dim"))
    children = child_identities(graph, identity)
    if children and depth == 0:
        hidden_identities = reachable_unique_identities(graph, children, exclude=shown)
        hidden = len(hidden_identities)
        if hidden:
            folded_label = Text(f"… {hidden} transitive dependencies folded", style="dim italic")
            append_folded_change_badges(folded_label, graph, hidden_identities, changes)
            branch.add(folded_label)
            return
        for child in children:
            add_dependency(
                branch,
                parent=identity,
                identity=child,
                graph=graph,
                depth=depth,
                shown=shown,
                changes=changes,
            )
        return
    next_depth = None if depth is None else depth - 1
    for child in children:
        add_dependency(
            branch,
            parent=identity,
            identity=child,
            graph=graph,
            depth=next_depth,
            shown=shown,
            changes=changes,
        )


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
    overview.add_row("Reason", reason_text(stage.reason))
    overview.add_row("Needs", ", ".join(stage.needs) if stage.needs else "-")
    if show_keys:
        overview.add_row("Decision key", stage.decision_key or "unavailable")
        overview.add_row("Stored key", stage.stored_key or "-")
    overview.add_row(
        "Dependencies",
        f"{stage.direct_count} direct · {stage.total_count} total · {stage.broad_count} broad",
    )

    content: list[RenderableType] = [overview]
    if stage.key_inputs is None:
        content.extend(
            [Text(), Text(f"Key inputs unavailable: {stage.unavailable_reason}", style="dim")]
        )
    else:
        content.extend([Text(), Text("Key inputs", style="bold"), key_inputs_table(stage)])
    console.print(Panel(Group(*content), border_style="dim"))

    root_label = Text.assemble(
        ("stage  ", "varve.dependency.stage"),
        (stage.name, "bold"),
        (f"  [{stage.direct_count} direct · {stage.total_count} total]", "dim"),
    )
    changes = stage.source_changes if show_keys else {}
    append_change_badge(root_label, changes.get("stage"))
    root = Tree(root_label, guide_style="dim")
    shown: set[str] = set()
    for identity in stage.source_dependencies.direct:
        add_dependency(
            root,
            parent="stage",
            identity=identity,
            graph=stage.source_dependencies,
            depth=depth,
            shown=shown,
            changes=changes,
        )
    title = (
        "Source dependencies · full tree"
        if depth is None
        else "Source dependencies · direct + one level"
        if depth == 1
        else "Source dependencies · folded"
    )
    removed = sorted(
        component
        for component, change in changes.items()
        if change == "removed" and component != "stage"
    )
    dependency_content: RenderableType = root
    if removed:
        removed_tree = Tree(Text("Removed source dependencies", style="bold"))
        visible_removed = removed if depth is None else removed[:_REMOVED_PREVIEW_LIMIT]
        for component in visible_removed:
            removed_tree.add(removed_dependency_label(component))
        hidden_removed = len(removed) - len(visible_removed)
        if hidden_removed:
            removed_tree.add(
                Text(
                    f"… {hidden_removed} more removed dependencies; run with --all or --deps-all",
                    style="dim italic",
                )
            )
        dependency_content = Group(root, Text(), removed_tree)
    console.print(Panel(dependency_content, title=title, title_align="left", border_style="dim"))
    if depth == 0:
        console.print(
            "[dim]Run with[/] [bold]--expand[/] [dim]or[/] [bold]--deps[/] "
            "[dim]to show one dependency level, or[/] [bold]--all[/] [dim]or[/] "
            "[bold]--deps-all[/] [dim]for the full tree.[/]"
        )
    elif depth == 1:
        console.print(
            "[dim]Run with[/] [bold]--all[/] [dim]or[/] [bold]--deps-all[/] "
            "[dim]for the full dependency tree.[/]"
        )
    console.print(
        "[dim]Auto dependencies are best effort. "
        "Dynamic calls and runtime dispatch are not inferred.[/]"
    )


def render_status(
    console: Console,
    status: PipelineStatus,
    *,
    view: Literal["summary", "cells", "detail"],
    dependency_depth: int | None = 0,
) -> None:
    if view == "summary":
        render_pipeline_summary(console, status)
        return
    if view == "cells":
        render_expanded_groups(console, status)
        return
    for index, stage in enumerate(status.stages):
        if index:
            console.print()
        render_stage_status(
            console,
            stage,
            depth=dependency_depth,
            show_keys=dependency_depth != 0,
        )
