"""Discovery metadata and shared-status wrappers for the dashboard."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from varve.engine.state import EffectiveStatus
from varve.status import PipelineStatus

ErrorPhase = Literal["manifest", "import", "resolve", "evaluate"]


class StateError(BaseModel):
    phase: ErrorPhase
    message: str


class PipelineEntry(BaseModel):
    output_root: Path
    pipeline_id: str
    pipeline_name: str | None
    branch: str
    module: str | None = None
    manifest_error: str | None = None
    temporary: bool = False


class PipelineState(BaseModel):
    """Discovery metadata around the canonical shared pipeline status."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    entry: PipelineEntry
    pipeline_status: PipelineStatus | None = None
    error: StateError | None = None

    @property
    def stages(self):
        return () if self.pipeline_status is None else self.pipeline_status.stages

    @property
    def status(self) -> EffectiveStatus:
        return "error" if self.error is not None else self._status.status

    @property
    def complete(self) -> bool:
        return self.error is None and self._status.complete

    @property
    def duration(self) -> float | None:
        return None if self.pipeline_status is None else self.pipeline_status.duration

    @property
    def last_run(self) -> datetime | None:
        return None if self.pipeline_status is None else self.pipeline_status.last_run

    @property
    def _status(self) -> PipelineStatus:
        if self.pipeline_status is None:
            raise RuntimeError("Pipeline state has no exact status")
        return self.pipeline_status
