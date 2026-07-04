from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel

from varve import Pipeline, stage
from varve.engine.runner import run


class Config(BaseModel):
    pass


class LogExperiment(Pipeline):
    Config = Config

    @classmethod
    def default_output_root(cls, config: Config) -> Path:
        return Path("varve-test-output")

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text("sample", encoding="utf-8")


def test_runner_emits_stage_level_logs(tmp_path: Path, caplog) -> None:
    caplog.set_level(logging.INFO, logger="varve")
    run(LogExperiment, Config(), cli_out=tmp_path)
    messages = [record.getMessage() for record in caplog.records]
    assert "plan: sample" in messages
    assert any("[sample] run · no cache" in message for message in messages)
    assert any("[sample] done" in message for message in messages)
    assert not any("sha256:" in message for message in messages)
