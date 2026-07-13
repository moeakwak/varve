"""Branch-independent pipeline structure rendering."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from varve.pipeline import Pipeline


def render_structure(console: Console, pipeline: type[Pipeline]) -> None:
    """Render stage templates without resolving a branch or probing a store."""

    table = Table(box=None)
    table.add_column("STAGE")
    table.add_column("KIND")
    table.add_column("NEEDS")
    table.add_column("MATRIX")
    stages = pipeline.stages()
    for name in pipeline.topo_order():
        spec = stages[name]
        needs = ", ".join(spec.needs) if spec.needs else "-"
        kind = "batch" if spec.kind == "batch" else "stage"
        axes = ", ".join(axis.name for axis in spec.matrix) if spec.matrix else "-"
        table.add_row(name, kind, needs, axes)
    console.print(table)
