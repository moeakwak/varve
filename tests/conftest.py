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
import subprocess
import sys
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from varve.engine import RunOptions, RunResult, run_pipeline
from varve.pipeline import Pipeline
from varve.store import layout
from varve.store.db import connect
from varve.store.repo import Repo, StageRunRow, _columns, _decode

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

    def repo(self, *, readonly: bool = True) -> Repo:
        return Repo(connect(layout.state_db_path(self.root), readonly=readonly))

    def stage_runs(self, repo: Repo) -> list[StageRunRow]:
        """Every ``stage_runs`` row, oldest first."""

        rows = repo.conn.execute(
            f"SELECT {', '.join(_columns(StageRunRow))} FROM stage_runs ORDER BY finished_at"
        )
        return [_decode(StageRunRow, row) for row in rows]

    def spawn_run(self, *argv: str, capture: bool = False) -> subprocess.Popen[str]:
        """``run --no-tui`` of the pipeline module in a child process."""

        output = subprocess.PIPE if capture else subprocess.DEVNULL
        return subprocess.Popen(
            [sys.executable, str(self.path), "run", "--no-tui", "--root", str(self.root), *argv],
            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
            stdout=output,
            stderr=output,
            text=True,
        )

    @staticmethod
    def wait_for(path: Path, proc: subprocess.Popen[str]) -> None:
        """Wait until ``path`` exists while ``proc`` is alive (20 s at most)."""

        deadline = time.monotonic() + 20
        while not path.exists():
            assert time.monotonic() < deadline and proc.poll() is None, proc.communicate()
            time.sleep(0.02)

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
