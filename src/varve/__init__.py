"""Public API for varve."""

from varve.context import Ctx
from varve.decorators import StageSpec, batch_stage, stage
from varve.experiment import Experiment
from varve.keyspec import JSON, KeySpec

__all__ = [
    "Ctx",
    "Experiment",
    "JSON",
    "KeySpec",
    "StageSpec",
    "batch_stage",
    "stage",
]

__version__ = "0.1.0"
