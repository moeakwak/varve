"""Rich rendering for structured pipeline status."""

from __future__ import annotations

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from varve.keying.dependencies import DependencyNode, SourceDependencies
from varve.status import PipelineStatus, StageStatus
from varve.style import format_elapsed, status_text

_COMPACT_REASON_PREFIXES = (
    ("global referenced by ", "global reference"),
    ("closure referenced by ", "closure reference"),
    ("default value declared by ", "default value"),
    ("module attribute referenced by ", "module attribute"),
    ("base class of ", "base class"),
)


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


def render_pipeline_summary(console: Console, status: PipelineStatus) -> None:
    console.print(
        Text.assemble(
            ("Pipeline status", "varve.dependency.stage"),
            (f"  branch {status.branch} · output {status.output_root}", "dim"),
        )
    )
    console.print()
    fold_core = console.width < 80
    core_overflow = "fold" if fold_core else "ignore"
    table = Table(box=None, padding=(0, 1), header_style="bold")
    table.add_column(
        "STAGE",
        style="bold",
        no_wrap=not fold_core,
        overflow=core_overflow,
    )
    table.add_column("STATUS", no_wrap=not fold_core, overflow=core_overflow)
    table.add_column(
        "DURATION",
        justify="right",
        no_wrap=not fold_core,
        overflow=core_overflow,
    )
    table.add_column("NEEDS", style="dim", overflow="fold")
    table.add_column("SOURCE DEPENDENCIES", overflow="fold")
    table.add_column("REASON", overflow="fold")
    for stage in status.stages:
        dependency_summary = Text()
        dependency_summary.append(f"{stage.direct_count} direct", style="varve.dependency.function")
        dependency_summary.append(f" · {stage.total_count} total", style="dim")
        if stage.broad_count:
            dependency_summary.append(
                f" · {stage.broad_count} broad", style="varve.dependency.broad"
            )
        table.add_row(
            stage.name,
            status_text(stage.status),
            format_elapsed(stage.duration),
            format_needs(stage.needs),
            dependency_summary,
            stage.reason,
        )
    console.print(table)
    console.print()
    console.print(
        "[dim]Dependencies are folded. Run[/] [bold]status STAGE[/] "
        "[dim]for one stage, then add[/] [bold]--expand[/] [dim]or[/] "
        "[bold]--all[/][dim].[/]"
    )


def child_identities(graph: SourceDependencies, parent: str) -> tuple[str, ...]:
    return tuple(sorted({edge.child for edge in graph.edges if edge.parent == parent}))


def edge_reasons(graph: SourceDependencies, parent: str, child: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            {edge.reason for edge in graph.edges if edge.parent == parent and edge.child == child}
        )
    )


def reachable_unique_count(
    graph: SourceDependencies,
    roots: tuple[str, ...],
    *,
    exclude: set[str],
) -> int:
    seen = set(exclude)
    stack = list(roots)
    count = 0
    while stack:
        identity = stack.pop()
        if identity in seen:
            continue
        seen.add(identity)
        count += 1
        stack.extend(child_identities(graph, identity))
    return count


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


def dependency_label(node: DependencyNode, *, qualified_name: str) -> Text:
    label = Text()
    label.append(node.kind, style=f"varve.dependency.{node.kind}")
    label.append("  ")
    label.append(qualified_name, style="bold")
    label.append(f"  [{node.origin}]", style="varve.dependency.metadata")
    if node.scope:
        label.append(f"  [{node.scope}]", style="varve.dependency.broad")
    return label


def add_dependency(
    tree: Tree,
    *,
    parent: str,
    identity: str,
    graph: SourceDependencies,
    depth: int | None,
    shown: set[str],
) -> None:
    node = graph.nodes[identity]
    display_name = relative_qualified_name(
        graph,
        parent=parent,
        child=identity,
    )
    if identity in shown:
        reference = tree.add(Text(f"↳ {display_name} already shown", style="dim"))
        for reason in edge_reasons(graph, parent, identity):
            reference.add(Text(compact_reason(reason), style="dim"))
        return
    shown.add(identity)
    branch = tree.add(dependency_label(node, qualified_name=display_name))
    for reason in edge_reasons(graph, parent, identity):
        branch.add(Text(compact_reason(reason), style="dim"))
    children = child_identities(graph, identity)
    if children and depth == 0:
        hidden = reachable_unique_count(graph, children, exclude=shown)
        if hidden:
            branch.add(Text(f"… {hidden} transitive dependencies folded", style="dim italic"))
            return
        for child in children:
            add_dependency(
                branch,
                parent=identity,
                identity=child,
                graph=graph,
                depth=depth,
                shown=shown,
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
    overview.add_row("Reason", stage.reason)
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

    root = Tree(
        Text.assemble(
            ("stage  ", "varve.dependency.stage"),
            (stage.name, "bold"),
            (f"  [{stage.direct_count} direct · {stage.total_count} total]", "dim"),
        ),
        guide_style="dim",
    )
    shown: set[str] = set()
    for identity in stage.source_dependencies.direct:
        add_dependency(
            root,
            parent="stage",
            identity=identity,
            graph=stage.source_dependencies,
            depth=depth,
            shown=shown,
        )
    title = (
        "Source dependencies · full tree"
        if depth is None
        else "Source dependencies · direct + one level"
        if depth == 1
        else "Source dependencies · folded"
    )
    console.print(Panel(root, title=title, title_align="left", border_style="dim"))
    if depth == 0:
        console.print(
            "[dim]Run with[/] [bold]--expand[/] [dim]to show one dependency level, "
            "or[/] [bold]--all[/] [dim]for the full tree.[/]"
        )
    elif depth == 1:
        console.print("[dim]Run with[/] [bold]--all[/] [dim]for the full dependency tree.[/]")
    console.print(
        "[dim]Auto dependencies are best effort. "
        "Dynamic calls and runtime dispatch are not inferred.[/]"
    )


def render_status(
    console: Console,
    status: PipelineStatus,
    *,
    stage: str | None,
    depth: int | None,
) -> None:
    if stage is None and depth == 0:
        render_pipeline_summary(console, status)
        return
    selected = (
        status.stages
        if stage is None
        else tuple(item for item in status.stages if item.name == stage)
    )
    for index, item in enumerate(selected):
        if index:
            console.print()
        render_stage_status(console, item, depth=depth, show_keys=depth != 0)
