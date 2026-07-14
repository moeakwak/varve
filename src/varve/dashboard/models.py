"""Discovery metadata and shared-status wrappers for the dashboard."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal, NamedTuple

from varve.engine.state import EffectiveStatus
from varve.status import PipelineStatus

ErrorPhase = Literal["manifest", "import", "resolve", "evaluate"]


class StateError(NamedTuple):
    phase: ErrorPhase
    message: str


class PipelineEntry(NamedTuple):
    output_root: Path
    pipeline_id: str
    pipeline_name: str | None
    branch: str
    module: str | None = None
    manifest_error: str | None = None


class PipelineState(NamedTuple):
    """Discovery metadata around the canonical shared pipeline status."""

    entry: PipelineEntry
    pipeline_status: PipelineStatus | None = None
    error: StateError | None = None

    @property
    def stages(self):
        return () if self.pipeline_status is None else self.pipeline_status.stages

    @property
    def status(self) -> EffectiveStatus:
        return "error" if self.pipeline_status is None else self.pipeline_status.status

    @property
    def complete(self) -> bool:
        return (
            self.pipeline_status is not None
            and self.error is None
            and self.pipeline_status.complete
        )

    @property
    def duration(self) -> float | None:
        return None if self.pipeline_status is None else self.pipeline_status.duration

    @property
    def last_run(self) -> datetime | None:
        return None if self.pipeline_status is None else self.pipeline_status.last_run
