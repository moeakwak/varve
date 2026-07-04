from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from varve import KeySpec, Pipeline, stage
from varve.engine.runner import run


class Config(BaseModel):
    token: str = "main"


class Args(BaseModel):
    src: Path


class BranchExperiment(Pipeline):
    Config = Config
    Args = Args

    @classmethod
    def default_output_root(cls, config: Config) -> Path:
        return Path("varve-test-output")

    @stage(
        produces="copy.txt",
        key=KeySpec(files={"src": lambda ctx: ctx.args.src}),
    )
    def copy(self, ctx):
        (ctx.out / "copy.txt").write_text(
            f"{ctx.config.token}:{ctx.args.src.read_text(encoding='utf-8')}",
            encoding="utf-8",
        )


def test_runner_uses_branch_output_root_and_passes_args_to_stage_and_keying(
    tmp_path: Path,
) -> None:
    src = tmp_path / "input.txt"
    src.write_text("payload", encoding="utf-8")
    out_base = tmp_path / "out"

    run(
        BranchExperiment,
        Config(token="branch"),
        args=Args(src=src),
        cli_out=out_base,
        branch="exp1",
    )

    assert (out_base / "exp1" / "copy.txt").read_text(encoding="utf-8") == "branch:payload"


def test_runner_uses_temporary_branch_output_root(tmp_path: Path) -> None:
    src = tmp_path / "input.txt"
    src.write_text("payload", encoding="utf-8")
    out_base = tmp_path / "out"

    run(
        BranchExperiment,
        Config(),
        args=Args(src=src),
        cli_out=out_base,
        branch="quick",
        is_temporary=True,
    )

    assert (out_base / ".tmp" / "quick" / "copy.txt").exists()
