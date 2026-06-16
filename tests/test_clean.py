from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from varve import Experiment, stage
from varve.clean import _validate_destructive, clean
from varve.ledger import Ledger
from varve.lock import OutputLock
from varve.models import ProducedPath
from varve.runner import run


class Config(BaseModel):
    out: Path


class CleanExperiment(Experiment):
    Config = Config

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text("sample", encoding="utf-8")

    @stage(needs="sample", produces="summary.txt")
    def summarize(self, ctx):
        (ctx.out / "summary.txt").write_text("summary", encoding="utf-8")


def test_validate_destructive_rejects_dangerous_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _validate_destructive(Path("/"))
    with pytest.raises(ValueError):
        _validate_destructive(Path.home())
    with pytest.raises(ValueError):
        _validate_destructive(tmp_path, allowed_roots=[tmp_path / "other"])
    _validate_destructive(tmp_path / "out", allowed_roots=[tmp_path])


def test_clean_requires_manifest_anchor(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Missing varve manifest"):
        clean(CleanExperiment, Config(out=tmp_path), yes=True)


def test_clean_full_output_root(tmp_path: Path) -> None:
    run(CleanExperiment, Config(out=tmp_path))
    clean(CleanExperiment, Config(out=tmp_path), yes=True, allowed_roots=[tmp_path.parent])
    assert not tmp_path.exists()


def test_clean_target_keeps_upstream(tmp_path: Path) -> None:
    run(CleanExperiment, Config(out=tmp_path))
    clean(CleanExperiment, Config(out=tmp_path), target="summarize", yes=True)
    assert (tmp_path / "sample.txt").exists()
    assert not (tmp_path / "summary.txt").exists()


def test_clean_respects_output_lock(tmp_path: Path) -> None:
    run(CleanExperiment, Config(out=tmp_path))
    with OutputLock(Ledger(tmp_path).root):
        with pytest.raises(RuntimeError, match="already locked"):
            clean(CleanExperiment, Config(out=tmp_path), yes=True)


def test_clean_target_preflights_external_outputs(tmp_path: Path) -> None:
    run(CleanExperiment, Config(out=tmp_path))
    record = Ledger(tmp_path).read_success("summarize")
    assert record is not None
    assert record.produces is not None
    record.produces = [ProducedPath(path="/tmp/outside-varve.txt", kind="file")]
    Ledger(tmp_path).write_success(record)

    with pytest.raises(ValueError, match="outside root"):
        clean(CleanExperiment, Config(out=tmp_path), target="sample", yes=True)
    assert (tmp_path / "sample.txt").exists()
