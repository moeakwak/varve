"""Rich rendering of CLI command results."""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rich import box as rich_box
from rich.console import Console
from rich.segment import Segment, Segments
from rich.style import Style
from rich.table import Table
from rich.text import Text

from varve.graph import StageGraph
from varve.pipeline import Pipeline
from varve.ref import Ref
from varve.store import layout
from varve.store.db import connect
from varve.store.repo import Repo
from varve.style import STATUS_STYLES, format_ms, format_timestamp, status_text

if TYPE_CHECKING:  # the engine is imported by the commands that produce these views
    from varve.engine.clean import CleanPlan
    from varve.engine.refs import RefInfo
    from varve.engine.run import RunResult
    from varve.engine.service import InspectResult, ReuseResult
    from varve.status import PipelineView, StageView

STAGE_NAME_LIMIT = 32


def _header(title: str, view: PipelineView) -> Text:
    text = Text(title, style="bold")
    text.append(" · ", style="dim")
    text.append(view.pipeline, style="blue")
    text.append(f" @ {view.branch}", style="dim")
    if view.is_temp:
        text.append(" (temporary)", style="dim")
    return text


def _stage_summary(stage: StageView) -> Text:
    text = Text()
    if stage.status == "hit":
        text.append("✓ ", style=STATUS_STYLES["hit"])
    text.append(stage.summary(), style=STATUS_STYLES.get(stage.status, ""))
    return text


# -- status --------------------------------------------------------------------


REF_NOTE = (
    "Single-pipeline probing only checks the upstream's committed records; "
    "use `varve plan` / `varve run` for a whole-chain freshness check."
)


def render_refs(console: Console, refs: Sequence[RefInfo]) -> None:
    """Upstream records read through cross-pipeline Refs, with their last commit."""

    if not refs:
        return
    table = Table(title="Refs", title_justify="left", box=rich_box.SIMPLE_HEAD, show_edge=False)
    for column in ("Ref", "Upstream", "Units", "Last commit", "Git", "Root"):
        table.add_column(column, no_wrap=column in ("Ref", "Upstream"))
    for info in refs:
        git = "-"
        if info.git_commit:
            git = info.git_commit[:10] + ("*" if info.git_dirty else "")
        table.add_row(
            info.attr,
            f"{info.target}:{info.stage}@{info.branch}",
            f"{info.units} ok",
            format_timestamp(info.last_commit_at),
            git,
            str(info.root),
        )
    console.print(table)
    console.print(Text(REF_NOTE, style="dim"))


def render_status(
    console: Console, view: PipelineView, *, coords: Mapping[str, str], expand: bool
) -> None:
    console.print(_header("Status", view))
    table = Table(box=rich_box.SIMPLE_HEAD, show_edge=False)
    table.add_column("Stage", style="bold", no_wrap=True)
    table.add_column("Axes", style="dim")
    table.add_column("Status")
    for stage in view.stages:
        axes = ", ".join(name if kind == "static" else f"{name}*" for name, kind in stage.axes)
        table.add_row(stage.name, axes or "-", _stage_summary(stage))
    console.print(table)
    render_refs(console, view.refs)
    if not expand:
        return
    for stage in view.stages:
        units = [
            unit
            for unit in stage.units
            if all(f"{name}={item}" in unit.unit_key.split(",") for name, item in coords.items())
        ]
        if not units:
            continue
        unit_table = Table(title=stage.name, title_justify="left", box=rich_box.SIMPLE_HEAD)
        unit_table.add_column("Unit", no_wrap=True)
        unit_table.add_column("Decision")
        unit_table.add_column("Detail", style="dim")
        for unit in units:
            detail = ", ".join([*([unit.reason] if unit.reason else []), *unit.labels])
            unit_table.add_row(unit.display, status_text(unit.decision), detail)
        console.print(unit_table)


# -- plan ----------------------------------------------------------------------


def wrap_stage_name(name: str, *, limit: int = STAGE_NAME_LIMIT) -> tuple[str, ...]:
    """Split a stage name into at most two display lines."""

    if len(name) <= limit:
        return (name,)
    split_at = max(name.rfind("_", 0, limit), name.rfind("-", 0, limit)) + 1 or None
    if split_at is None:
        return (name[:limit], _truncate(name[limit:], limit))
    return (name[:split_at], _truncate(name[split_at:], limit))


def _truncate(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    head = limit // 2
    tail = limit - head - 1
    return f"{value[:head]}…{value[-tail:] if tail else ''}"


def _node_label(stage: StageView) -> Text:
    label = Text()
    for index, line in enumerate(wrap_stage_name(stage.name)):
        if index:
            label.append("\n")
        label.append(line, style="bold")
    label.append("\n")
    if stage.status == "hit":
        label.append(f"✓ {stage.total}" if stage.total != 1 else "✓", style=STATUS_STYLES["hit"])
    elif stage.status == "awaiting":
        label.append(f"awaiting {stage.awaiting}", style=STATUS_STYLES["awaiting"])
    else:
        label.append(f"{stage.hit}/{stage.total}", style=STATUS_STYLES.get(stage.status, ""))
        if stage.counts.get("failed"):
            label.append(f" · ✕ {stage.counts['failed']}", style=STATUS_STYLES["failed"])
    return label


def render_plan(
    console: Console,
    view: PipelineView,
    graph: StageGraph,
    *,
    only: str | None = None,
    upto: str | None = None,
    downstream: str | None = None,
) -> None:
    selected = list(graph.order)
    for name in (only, upto, downstream):
        if name is not None and name not in graph.nodes:
            raise ValueError(f"Unknown stage {name!r}; stages: {selected}")
    if only:
        selected = [only]
    elif upto:
        selected = [*graph.ancestors(upto), upto]
    elif downstream:
        selected = [downstream, *graph.descendants(downstream)]
    stages = [view.stage(name) for name in selected]
    console.print(_header("Plan", view))
    console.print()
    try:
        lines = console.render_lines(_plan_graph(stages, console), pad=False)
    except Exception:  # netext layout limits: fall back to a list
        for stage in stages:
            deps = f" ← {', '.join(stage.deps)}" if stage.deps else ""
            console.print(Text(stage.name, style="bold") + Text(deps, style="dim"))
            console.print(Text("  ") + _stage_summary(stage))
    else:
        rows: list[Segment] = []
        for line in compact_rows(lines):
            rows.extend([*line, Segment.line()])
        console.print(Segments(rows))
    external = [(name, stage_refs(graph, name)) for name in selected]
    external = [(name, labels) for name, labels in external if labels]
    if external:
        console.print(Text("External inputs (Refs)", style="bold"))
        for name, labels in external:
            console.print(Text(f"  {name}", style="bold") + Text(f" ← {', '.join(labels)}"))


VERTICAL_EDGE = frozenset("│ ")


def compact_rows(lines: list[list[Segment]], *, keep: int = 1) -> list[list[Segment]]:
    """Collapse runs of rows holding only vertical edges (or nothing) to ``keep`` rows.

    The Sugiyama layout of netext leaves several rows between layers and has no spacing
    option; a row made of ``│`` only continues every edge straight down, so dropping
    repeats of it keeps all edges connected.
    """

    compacted: list[list[Segment]] = []
    run = 0
    for line in lines:
        text = "".join(segment.text for segment in line if not segment.control)
        if text.strip() and set(text) - VERTICAL_EDGE:
            run = 0
        else:
            run += 1
            if run > keep:
                continue
        compacted.append(line)
    return compacted


def _plan_graph(stages: list[StageView], console: Console) -> Any:
    from netext import ArrowTip, ConsoleGraph, EdgeRoutingMode, EdgeSegmentDrawingMode
    from netext.layout_engines import LayoutDirection, SugiyamaLayout

    names = {stage.name for stage in stages}
    nodes: dict[Hashable, dict[str, Any]] = {
        stage.name: {
            "$label": _node_label(stage),
            "$content-renderer": lambda _name, data, _style: data["$label"],
            "$shape": "box",
            "$box-type": rich_box.ROUNDED,
            "$padding": (0, 1),
        }
        for stage in stages
    }
    edge: dict[str, Any] = {
        "$edge-routing-mode": EdgeRoutingMode.ORTHOGONAL,
        "$edge-segment-drawing-mode": EdgeSegmentDrawingMode.BOX,
        "$end-arrow-tip": ArrowTip.ARROW,
        "$style": Style(dim=True),
    }
    edges = [(dep, stage.name, edge) for stage in stages for dep in stage.deps if dep in names]
    return ConsoleGraph(
        nodes, edges, console=console, layout_engine=SugiyamaLayout(LayoutDirection.TOP_DOWN)
    )


# -- ls ------------------------------------------------------------------------


def ref_label(ref: Ref) -> str:
    """``@target:stage``, with ``@branch,...`` appended unless it reads ``main`` only."""

    label = f"@{ref.describe()}"
    if ref.branches != ("main",):
        label += f"@{','.join(ref.branches)}"
    return label


def stage_refs(graph: StageGraph, name: str) -> list[str]:
    """Labels of the Refs a stage reads (key inputs and dynamic-axis resolvers)."""

    node = graph.nodes[name]
    return [ref_label(graph.refs[ref]) for ref in dict.fromkeys((*node.refs, *node.resolver_refs))]


def render_ls(console: Console, pipeline: type[Pipeline], root: Path | None) -> None:
    graph = pipeline.graph()
    title = Text("Stages", style="bold")
    title.append(f" · {pipeline.selector()}", style="blue")
    console.print(title)
    table = Table(box=rich_box.SIMPLE_HEAD, show_edge=False)
    for column in (
        "Stage",
        "Axes",
        "Needs",
        "Storage",
        "Produces",
        "Publish",
        "Version",
        "Workers",
    ):
        table.add_column(
            column, no_wrap=column == "Stage", overflow="fold" if column == "Needs" else "ellipsis"
        )
    for name, node in graph.nodes.items():
        spec = node.spec
        table.add_row(
            Text(name, style="bold"),
            ", ".join(axis.name if axis.is_static else f"{axis.name}*" for axis in node.axes)
            or "-",
            ", ".join([*node.deps, *stage_refs(graph, name)]) or "-",
            node.storage,
            ", ".join(spec.produces) if spec.produces else "dir",
            ", ".join(entry.dst for entry in spec.publish) or "-",
            spec.version or "-",
            str(node.workers) if isinstance(node.workers, int) else "callable",
        )
    console.print(table)
    from varve.engine.clean import pipeline_roots

    roots = pipeline_roots(pipeline, root)
    db_path = layout.state_db_path(roots.root)
    if not db_path.exists():
        console.print(Text(f"No state yet at {db_path}", style="dim"))
        return
    repo = Repo(connect(db_path, readonly=True))
    try:
        branches = Table(title="Branches", title_justify="left", box=rich_box.SIMPLE_HEAD)
        for column in ("Branch", "Kind", "Units", "Last run"):
            branches.add_column(column)
        for row in repo.list_branches():
            counts: dict[str, int] = {}
            for by_status in repo.unit_counts(row.branch_id).values():
                for status, count in by_status.items():
                    counts[status] = counts.get(status, 0) + count
            units = Text(", ".join(f"{count} {status}" for status, count in sorted(counts.items())))
            runs = repo.runs(row.branch_id, limit=1)
            last = Text("-")
            if runs:
                run = runs[0]
                last = status_text(run.status) + Text(
                    f" {format_timestamp(run.started_at)} · {format_ms(run.total_wall_ms)}",
                    style="dim",
                )
            kind = "temporary" if row.is_temp else "manual" if row.manual else "persistent"
            branches.add_row(row.name, kind, units, last)
        console.print(branches)
    finally:
        repo.conn.close()


# -- inspect -------------------------------------------------------------------


def render_inspect(console: Console, result: InspectResult, *, sources: bool, log: bool) -> None:
    _render_inspect(console, result, sources=sources, log=log)
    render_refs(console, result.refs)


def _render_inspect(console: Console, result: InspectResult, *, sources: bool, log: bool) -> None:
    view = result.view
    if view is None:
        if not result.units:
            console.print(f"No unit of {result.stage!r} matches (or the stage is awaiting).")
        else:
            table = Table(title=f"{result.stage}: {len(result.units)} units", title_justify="left")
            table.add_column("Unit")
            table.add_column("Decision")
            table.add_column("Reason", style="dim")
            for unit_key, decision, reason in result.units:
                table.add_row(unit_key or "-", status_text(decision), reason or "")
            console.print(table)
            console.print(Text("Narrow with STAGE@AXIS=ID,... to inspect one unit.", style="dim"))
        if sources and result.source is not None:
            _render_sources(console, result.source)
        return
    title = Text(f"{view.stage}", style="bold")
    if view.unit_key:
        title.append(f"@{view.unit_key}", style="cyan")
    title.append("  ")
    title.append_text(status_text(view.decision))
    reasons = list(view.reasons) or ([view.reason] if view.reason else [])
    details = [*reasons, *view.labels]
    if details:
        title.append(f" · {', '.join(details)}", style="dim")
    console.print(title)
    row = view.row
    info = Table.grid(padding=(0, 2))
    info.add_column(style="dim")
    info.add_column()
    info.add_row("unit dir", str(view.unit_dir))
    if view.artifact_path is not None:
        info.add_row("artifact", str(view.artifact_path))
    if row is not None:
        info.add_row("stored status", row.status)
        if row.artifact_fp:
            info.add_row(
                "artifact fp",
                f"{row.artifact_fp[:16]} ({row.artifact_files} files, {row.artifact_bytes} bytes)",
            )
        info.add_row("input key", row.input_key[:16])
        info.add_row(
            "config access",
            "whole Config"
            if row.config_access is None
            else ", ".join(row.config_access) or "(none)",
        )
        if row.duration_ms is not None:
            info.add_row("duration", format_ms(row.duration_ms))
        if row.metrics:
            info.add_row("metrics", ", ".join(f"{k}={v}" for k, v in row.metrics.items()))
        if row.linked_from:
            info.add_row("linked from", row.linked_from)
        if row.last_run_id:
            info.add_row("last run", row.last_run_id)
    if view.log_path is not None:
        info.add_row("log", str(view.log_path))
    console.print(info)
    changed = [diff for diff in view.segments if diff.changed]
    if changed:
        console.print(Text("Changed key segments", style="bold"))
        for diff in changed:
            console.print(Text(f"  {diff.segment}", style="varve.diff.segment"))
            for line in diff.details:
                console.print(Text(f"    {line}", style="varve.diff.changed"))
    source = view.source
    if source.changed or source.added or source.removed:
        console.print(Text("Changed source symbols", style="bold"))
        for kind, names in (
            ("changed", source.changed),
            ("added", source.added),
            ("removed", source.removed),
        ):
            for name in names:
                console.print(
                    Text(
                        f"  {kind[0] if kind != 'changed' else '~'} {name}",
                        style=f"varve.diff.{kind}",
                    )
                )
    if row is not None and row.error_traceback:
        console.print(Text("Last failure", style="bold red"))
        console.print(Text(row.error_traceback.rstrip(), style="red"))
    if sources and result.source is not None:
        _render_sources(console, result.source)
    if log:
        if view.log_path is None:
            console.print(Text("No captured log for this unit.", style="dim"))
        else:
            console.print(Text(f"── {view.log_path}", style="dim"))
            console.print(
                view.log_path.read_text(encoding="utf-8", errors="replace"),
                markup=False,
                highlight=False,
            )


def _render_sources(console: Console, detail: Mapping[str, Any]) -> None:
    head = Text("Sources", style="bold")
    head.append(f" · {detail.get('mode')} of {detail.get('file')}", style="dim")
    console.print(head)
    if detail.get("fallback_reason"):
        console.print(Text(f"  whole-file fallback: {detail['fallback_reason']}", style="yellow"))
    for name in detail.get("symbols", {}):
        console.print(Text(f"  {name}", style="varve.diff.symbol"))
    for name in detail.get("external", {}):
        console.print(Text(f"  {name}", style="varve.diff.metadata"))


# -- run / reuse / clean -------------------------------------------------------


def render_run_summary(console: Console, result: RunResult) -> None:
    problems = [o for o in result.stages.values() if o.status not in ("ok",)]
    for outcome in problems:
        text = Text(f"{outcome.name}: ", style="bold")
        text.append_text(status_text(outcome.status))
        if outcome.detail:
            text.append(f" · {outcome.detail}", style="dim")
        console.print(text)


def render_reuse(console: Console, result: ReuseResult) -> None:
    text = Text(f"Reused {result.reused} unit(s) of {result.stage!r}", style="bold")
    if result.already:
        text.append(f" · {result.already} already hit", style="dim")
    console.print(text)
    for reason, count in result.not_reusable.items():
        console.print(Text(f"  {count} not reusable: {reason}", style="yellow"))


def render_clean_plan(console: Console, plan: CleanPlan) -> None:
    console.print(Text(f"Clean {plan.description}:", style="bold"))
    for path in plan.paths[:20]:
        console.print(f"  {path}")
    if len(plan.paths) > 20:
        console.print(Text(f"  … and {len(plan.paths) - 20} more", style="dim"))
    if plan.rows:
        console.print(Text(f"  {plan.rows} unit row(s)", style="dim"))
