"""Rich rendering for structured pipeline details."""

from __future__ import annotations

from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

from varve.details import PipelineDetails, StageDetails
from varve.keying.dependencies import DependencyNode, SourceDependencies
from varve.style import status_text


def render_pipeline_summary(console: Console, details: PipelineDetails) -> None:
    console.print(
        Text.assemble(
            ("Pipeline details", "varve.dependency.stage"),
            (f"  branch {details.branch} · output {details.output_root}", "dim"),
        )
    )
    console.print()
    table = Table(box=None, padding=(0, 2), header_style="bold")
    table.add_column("STAGE", style="bold")
    table.add_column("STATUS")
    table.add_column("NEEDS", style="dim")
    table.add_column("KEY", style="dim")
    table.add_column("SOURCE DEPENDENCIES")
    table.add_column("REASON")
    for stage in details.stages:
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
            ", ".join(stage.needs) if stage.needs else "-",
            stage.decision_key[:8] if stage.decision_key is not None else "-",
            dependency_summary,
            stage.reason,
        )
    console.print(table)
    console.print()
    console.print(
        "[dim]Dependencies are folded. Run[/] [bold]details STAGE[/] "
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


def key_inputs_table(stage: StageDetails) -> Table:
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


def dependency_label(node: DependencyNode) -> Text:
    label = Text()
    label.append(node.kind, style=f"varve.dependency.{node.kind}")
    label.append("  ")
    label.append(node.qualified_name, style="bold")
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
    if identity in shown:
        reference = tree.add(Text(f"↳ {node.qualified_name} already shown", style="dim"))
        for reason in edge_reasons(graph, parent, identity):
            reference.add(Text(reason, style="dim"))
        return
    shown.add(identity)
    branch = tree.add(dependency_label(node))
    for reason in edge_reasons(graph, parent, identity):
        branch.add(Text(reason, style="dim"))
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


def render_stage_details(console: Console, stage: StageDetails, *, depth: int | None) -> None:
    heading = Text(stage.name, style="varve.dependency.stage")
    heading.append("  ")
    heading.append_text(status_text(stage.status))
    console.print(heading)
    console.print()

    overview = Table(box=None, show_header=False, padding=(0, 2))
    overview.add_column(style="dim", no_wrap=True)
    overview.add_column()
    overview.add_row("Reason", stage.reason)
    overview.add_row("Needs", ", ".join(stage.needs) if stage.needs else "-")
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


def render_details(
    console: Console,
    details: PipelineDetails,
    *,
    stage: str | None,
    depth: int | None,
) -> None:
    if stage is None and depth == 0:
        render_pipeline_summary(console, details)
        return
    selected = (
        details.stages
        if stage is None
        else tuple(item for item in details.stages if item.name == stage)
    )
    for index, item in enumerate(selected):
        if index:
            console.print()
        render_stage_details(console, item, depth=depth)
