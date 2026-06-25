"""Rich renderers for dashboard states."""

from __future__ import annotations

from datetime import datetime

from rich.console import Console
from rich.table import Table
from rich.text import Text

from varve.dashboard.models import ExperimentState, OverallStatus, StageState, StageStatus

_STATUS_STYLES: dict[StageStatus | OverallStatus, str] = {
    "ok": "green",
    "artifact-missing": "yellow",
    "interrupted": "yellow",
    "corrupt": "red",
    "empty": "dim",
}


def render_overview(states: list[ExperimentState]) -> None:
    console = Console(highlight=False)
    table = Table()
    table.add_column("EXPERIMENT")
    table.add_column("BRANCH")
    table.add_column("OVERALL")
    table.add_column("STAGES")
    table.add_column("LAST RUN")

    for state in sorted(states, key=lambda item: (item.entry.experiment_id, item.entry.branch)):
        ok_count = sum(1 for stage in state.stages if stage.status == "ok")
        table.add_row(
            state.entry.experiment_id,
            state.entry.branch,
            _status_text(state.overall),
            f"{ok_count}/{len(state.stages)}",
            _format_datetime(_last_run(state.stages)),
        )
    console.print(table)


def render_detail(state: ExperimentState) -> None:
    console = Console(highlight=False)
    experiment_name = state.entry.experiment_name or state.entry.experiment_id
    console.print(f"Experiment: {state.entry.experiment_id}")
    # soft_wrap keeps long output-root paths on one line; rich would otherwise
    # hard-wrap them at the console width and split the path mid-string.
    console.print(f"Output root: {state.entry.output_root}", soft_wrap=True)
    console.print(f"Name: {experiment_name}")
    console.print("Overall: ", _status_text(state.overall), sep="")
    console.print()

    stage_by_name = {stage.name: stage for stage in state.stages}
    stage_table = Table(title="Stages")
    stage_table.add_column("STAGE")
    stage_table.add_column("STATUS")
    stage_table.add_column("ARTIFACTS")
    stage_table.add_column("COMMITTED")
    stage_table.add_column("UPSTREAMS")
    for name in state.order:
        stage = stage_by_name[name]
        stage_table.add_row(
            stage.name,
            _status_text(stage.status),
            _format_artifacts(stage),
            _format_datetime(stage.committed_at),
            ", ".join(stage.upstreams) if stage.upstreams else "-",
        )
    console.print(stage_table)
    console.print()

    console.print("Plan")
    if not state.order:
        console.print("  No recorded stages.")
        return
    nodes = set(stage_by_name)
    printed_any = False
    for name in state.order:
        stage = stage_by_name[name]
        upstreams = [upstream for upstream in stage.upstreams if upstream in nodes]
        if not upstreams:
            console.print(f"  root: {stage.name}")
            printed_any = True
            continue
        for upstream in upstreams:
            console.print(f"  {upstream} -> {stage.name}")
            printed_any = True
    if not printed_any:
        console.print("  No recorded dependencies.")


def _status_text(status: StageStatus | OverallStatus) -> Text:
    return Text(status, style=_STATUS_STYLES[status])


def _format_artifacts(stage: StageState) -> str:
    if not stage.artifacts:
        return "-"
    return ", ".join(
        f"{artifact.path} ({'ok' if artifact.exists else 'missing'})"
        for artifact in stage.artifacts
    )


def _format_datetime(value: datetime | None) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value is not None else ""


def _last_run(stages: list[StageState]) -> datetime | None:
    values = [stage.committed_at for stage in stages if stage.committed_at is not None]
    return max(values) if values else None
