"""Shared fixtures: pipeline modules written to a temp dir and imported by name.

Pipelines live in real modules on ``sys.path`` (not ``spec_from_file_location``)
so that spawned process-pool workers can import them, and so the source
closure analysis reads a real file. Every rewrite bumps the file's mtime by a
whole second so neither the bytecode cache nor the stat caches see stale data.
"""

from __future__ import annotations

import importlib
import itertools
import os
import sys
import textwrap
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from varve.engine import RunOptions, RunResult, run_pipeline
from varve.pipeline import Pipeline

_counter = itertools.count()


class Workspace:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.src = base / "src"
        self.src.mkdir()
        self.root = base / "root"
        self.module = f"varve_t{os.getpid()}_{next(_counter)}"
        self._tick = 1_700_000_000
        sys.path.insert(0, str(self.src))

    @property
    def path(self) -> Path:
        return self.src / f"{self.module}.py"

    def write(self, source: str, cls: str = "Demo") -> type[Pipeline]:
        """(Re)write the pipeline module and return its class ``cls``."""

        self.path.write_text(textwrap.dedent(source))
        self._tick += 10
        os.utime(self.path, (self._tick, self._tick))
        if self.module in sys.modules:
            module = importlib.reload(sys.modules[self.module])
        else:
            module = importlib.import_module(self.module)
        return getattr(module, cls)

    def run(self, pipeline: type[Pipeline], **kwargs: Any) -> RunResult:
        options = kwargs.pop("options", None) or RunOptions(tui=False)
        kwargs.setdefault("root", self.root)
        return run_pipeline(pipeline, options=options, **kwargs)

    def cli(self, pipeline: type[Pipeline], *argv: str) -> int:
        return pipeline.cli([*argv, "--root", str(self.root)])

    def close(self) -> None:
        sys.modules.pop(self.module, None)
        if str(self.src) in sys.path:
            sys.path.remove(str(self.src))


@pytest.fixture
def ws(tmp_path: Path) -> Iterator[Workspace]:
    workspace = Workspace(tmp_path)
    try:
        yield workspace
    finally:
        workspace.close()
