from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from varve.dashboard.cli import main as dashboard_main
from varve.engine import runner as runner_module
from varve.engine.review import validate_base_stage_targets
from varve.engine.runner import (
    ReviewRequiredError,
    _KeyingSession,
    probe_pipeline,
    record_source_review,
    run,
)
from varve.engine.state import SourceReviewState
from varve.store.store import Store


def _load_pipeline(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.Demo


def _source(body: str) -> str:
    return f"""from pathlib import Path
from pydantic import BaseModel
from varve import Pipeline, stage

class Config(BaseModel):
    pass

class Demo(Pipeline):
    Config = Config
    @stage(produces="artifact.txt")
    def build(self, ctx):
        {body}
"""


def _source_with_input(body: str) -> str:
    return f"""from pathlib import Path
from pydantic import BaseModel
from varve import Dependencies, Pipeline, stage

class Config(BaseModel):
    profile: str
    limit: int
    source: Path

class Demo(Pipeline):
    Config = Config
    @stage(
        produces="artifact.txt",
        depends=Dependencies(inputs={{"source": lambda ctx: ctx.config.source}}),
    )
    def build(self, ctx):
        {body}
"""


def _two_stage_source() -> str:
    return """from pathlib import Path
from pydantic import BaseModel
from varve import Dependencies, Pipeline, stage

class Config(BaseModel):
    pass

class Demo(Pipeline):
    Config = Config

    @stage(produces="one.txt", depends=Dependencies(sources=[Path("one_source.py")]))
    def one(self, ctx):
        (ctx.out / "one.txt").write_text("one", encoding="utf-8")

    @stage(produces="two.txt", depends=Dependencies(sources=[Path("two_source.py")]))
    def two(self, ctx):
        (ctx.out / "two.txt").write_text("two", encoding="utf-8")
"""


def _dependent_two_stage_source() -> str:
    return """from pathlib import Path
from pydantic import BaseModel
from varve import Dependencies, Pipeline, stage

class Config(BaseModel):
    pass

class Demo(Pipeline):
    Config = Config

    @stage(produces="upstream.txt", depends=Dependencies(review_sources=[Path("upstream.py")]))
    def upstream(self, ctx):
        (ctx.out / "upstream.txt").write_text("upstream", encoding="utf-8")

    @stage(
        needs="upstream",
        produces="target.txt",
        depends=Dependencies(review_sources=[Path("target.py")]),
    )
    def target(self, ctx):
        (ctx.out / "target.txt").write_text("target", encoding="utf-8")
"""


def _external_pending_with_evaluation_error_source() -> str:
    return """from pathlib import Path
from pydantic import BaseModel
from varve import Dependencies, Pipeline, stage

class Config(BaseModel):
    source: Path = Path("input.txt")

class Demo(Pipeline):
    Config = Config

    @stage(produces="upstream.txt", depends=Dependencies(review_sources=[Path("upstream.py")]))
    def upstream(self, ctx):
        (ctx.out / "upstream.txt").write_text("upstream", encoding="utf-8")

    @stage(
        needs="upstream",
        produces="target.txt",
        depends=Dependencies(inputs={"source": lambda ctx: ctx.config.source}),
    )
    def target(self, ctx):
        (ctx.out / "target.txt").write_text(
            ctx.config.source.read_text(encoding="utf-8"), encoding="utf-8"
        )
"""


def _forced_failure_source(*, hard_interrupt: bool = False) -> str:
    exception = "raise KeyboardInterrupt()" if hard_interrupt else 'raise RuntimeError("planned")'
    return _reviewable_source("2").replace(
        '(ctx.out / "artifact.txt").write_text("same", encoding="utf-8")',
        exception,
    )


def _forced_two_stage_source() -> str:
    return """from pathlib import Path
from pydantic import BaseModel
from varve import Dependencies, Pipeline, stage

class Config(BaseModel):
    pass

class Args(BaseModel):
    fail: bool = False

class Demo(Pipeline):
    Config = Config
    Args = Args

    @stage(produces="one.txt", depends=Dependencies(review_sources=[Path("shared.py")]))
    def one(self, ctx):
        if ctx.args.fail:
            raise RuntimeError("planned first-stage failure")
        (ctx.out / "one.txt").write_text("one", encoding="utf-8")

    @stage(
        needs="one",
        produces="two.txt",
        depends=Dependencies(review_sources=[Path("shared.py")]),
    )
    def two(self, ctx):
        (ctx.out / "two.txt").write_text("two", encoding="utf-8")

    @stage(needs="two", produces="three.txt")
    def three(self, ctx):
        (ctx.out / "three.txt").write_text("three", encoding="utf-8")
"""


def _force_partial_source() -> str:
    return """from pathlib import Path
from pydantic import BaseModel
from varve import Dependencies, Pipeline, batch_stage

class Config(BaseModel):
    special: str
    unused: str

class Args(BaseModel):
    fail: bool = False

class Demo(Pipeline):
    Config = Config
    Args = Args

    @batch_stage(depends=Dependencies(review_sources=[Path("helper.py")]))
    async def build(self, ctx):
        async for index, item in ctx.resume(range(3), progress=False):
            if index == 0:
                _ = ctx.config.special
            with (ctx.out / "calls.txt").open("a", encoding="utf-8") as stream:
                stream.write(f"{index}\\n")
            path = ctx.out / f"part-{index}.txt"
            path.write_text(str(item), encoding="utf-8")
            yield path
            if ctx.args.fail and index == 0:
                raise RuntimeError("planned")
"""


def _matrix_source() -> str:
    return """from pathlib import Path
from pydantic import BaseModel
from varve import Axis, Dependencies, Pipeline, matrix, stage

CELL = Axis("cell", ["a", "b"])

class Config(BaseModel):
    pass

class Demo(Pipeline):
    Config = Config

    @matrix(CELL)
    @stage(produces="artifact.txt", depends=Dependencies(review_sources=[Path("helper.py")]))
    def build(self, ctx, *, cell):
        ctx.cell_out.mkdir(parents=True, exist_ok=True)
        (ctx.cell_out / "artifact.txt").write_text(cell, encoding="utf-8")
"""


def _matrix_2d_source() -> str:
    return """from pathlib import Path
from pydantic import BaseModel
from varve import Axis, Dependencies, Pipeline, matrix, stage

BENCH = Axis("bench", ["a", "b"])
MODEL = Axis("model", ["small", "large"])

class Config(BaseModel):
    pass

class Demo(Pipeline):
    Config = Config

    @matrix(BENCH, MODEL)
    @stage(produces="artifact.txt", depends=Dependencies(review_sources=[Path("helper.py")]))
    def build(self, ctx, *, bench, model):
        ctx.cell_out.mkdir(parents=True, exist_ok=True)
        (ctx.cell_out / "artifact.txt").write_text(f"{bench}:{model}", encoding="utf-8")
"""


def _batch_source() -> str:
    return """from pathlib import Path
from pydantic import BaseModel
from varve import Dependencies, Pipeline, batch_stage

class Config(BaseModel):
    input: Path

class Args(BaseModel):
    fail_after: int | None = None

class Demo(Pipeline):
    Config = Config
    Args = Args

    @batch_stage(
        depends=Dependencies(
            inputs={"input": lambda ctx: ctx.config.input},
            review_sources=[Path("helper.py")],
        )
    )
    async def build(self, ctx):
        async for index, item in ctx.resume(range(3), progress=False):
            calls = ctx.out / "calls.txt"
            with calls.open("a", encoding="utf-8") as stream:
                stream.write(f"{index}\\n")
            path = ctx.out / f"part-{index}.txt"
            path.write_text(str(item), encoding="utf-8")
            yield path
            if ctx.args.fail_after is not None and index >= ctx.args.fail_after:
                raise RuntimeError("planned failure")
"""


def _reviewable_source(helper_value: str = "1") -> str:
    return f"""from pathlib import Path
from pydantic import BaseModel
from varve import Pipeline, stage

HELPER = {helper_value}

class Config(BaseModel):
    pass

class Demo(Pipeline):
    Config = Config
    @stage(produces="artifact.txt")
    def build(self, ctx):
        (ctx.out / "artifact.txt").write_text("same", encoding="utf-8")
"""


def test_source_change_requires_review_and_reuse_does_not_rewrite_success(tmp_path: Path) -> None:
    module_path = tmp_path / "demo.py"
    module_path.write_text(_reviewable_source("1"), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "review_demo")
    run(pipeline, pipeline.Config(), cli_out=tmp_path / "out")
    output_root = tmp_path / "out" / "main"
    before = (output_root / ".varve" / "stages" / "build.json").read_bytes()

    module_path.write_text(_reviewable_source("2"), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "review_demo")
    probe = probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)[0]
    assert probe.source_review == SourceReviewState("changed")
    assert probe.decision.status == "hit"
    with pytest.raises(ReviewRequiredError) as error:
        run(pipeline, pipeline.Config(), cli_out=tmp_path / "out")
    assert error.value.stages == ["build"]
    assert str(error.value) == ("Source review required for: build. Run reuse or invalidate first.")
    assert pipeline.cli(["run", "--out", str(tmp_path / "out")]) == 2

    assert pipeline.cli(["reuse", "--out", str(tmp_path / "out")]) == 0
    assert (output_root / ".varve" / "stages" / "build.json").read_bytes() == before
    assert probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)[
        0
    ].source_review == SourceReviewState("changed", "reuse")
    module_path.write_text(_reviewable_source("3"), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "review_demo")
    assert probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)[
        0
    ].source_review == SourceReviewState("changed")
    record_source_review(
        pipeline,
        pipeline.Config(),
        decision="invalidate",
        cli_out=tmp_path / "out",
    )
    invalidated = probe_pipeline(
        pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root
    )[0]
    assert invalidated.source_review == SourceReviewState("changed", "invalidate")
    assert invalidated.decision.status == "hit"
    assert invalidated.decision.reason == "hit"
    run(pipeline, pipeline.Config(), cli_out=tmp_path / "out")
    # Successful execution updates the Cell baseline but keeps the Stage ReviewRecord.
    assert Store(output_root).read_review("build") is not None
    assert (
        probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)[
            0
        ].source_review.relationship
        == "current"
    )


def test_pipeline_review_without_source_changes_is_a_successful_noop(tmp_path: Path) -> None:
    module_path = tmp_path / "no_review.py"
    module_path.write_text(_source('(ctx.out / "artifact.txt").write_text("same")'))
    pipeline = _load_pipeline(module_path, "no_review_demo")

    result = record_source_review(
        pipeline,
        pipeline.Config(),
        decision="reuse",
        cli_out=tmp_path / "out",
    )

    assert result.recorded == ()
    assert result.already_decided == ()
    assert result.did_not_need_review == ()
    assert result.groups == ()


def test_record_source_review_passes_shared_session_to_exact_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_path = tmp_path / "shared_session.py"
    module_path.write_text(_source('(ctx.out / "artifact.txt").write_text("same")'))
    pipeline = _load_pipeline(module_path, "shared_review_session_demo")
    session = _KeyingSession()
    original_probe = runner_module.probe_pipeline
    observed_sessions = []

    def tracking_probe(*args, **kwargs):
        observed_sessions.append(kwargs.get("_keying_session"))
        return original_probe(*args, **kwargs)

    monkeypatch.setattr(runner_module, "probe_pipeline", tracking_probe)
    record_source_review(
        pipeline,
        pipeline.Config(),
        decision="reuse",
        cli_out=tmp_path / "out",
        _keying_session=session,
    )

    assert observed_sessions == [session]


def test_docstring_change_is_deterministic_rerun(tmp_path: Path) -> None:
    module_path = tmp_path / "docstring_demo.py"
    module_path.write_text(
        _source('"""first"""\n        (ctx.out / "artifact.txt").write_text("same")')
    )
    pipeline = _load_pipeline(module_path, "docstring_demo")
    output_base = tmp_path / "out"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    module_path.write_text(
        _source('"""second"""\n        (ctx.out / "artifact.txt").write_text("same")')
    )
    pipeline = _load_pipeline(module_path, "docstring_demo")

    probe = probe_pipeline(
        pipeline,
        pipeline.Config(),
        args=pipeline.Args(),
        out=output_base / "main",
    )[0]

    assert probe.decision.status == "needs-run"
    assert probe.decision.reason == "source-changed"
    assert probe.source_review.relationship == "current"


def test_review_dependency_error_fails_before_writing_decision(tmp_path: Path) -> None:
    module_path = tmp_path / "review_error_demo.py"
    input_path = tmp_path / "input.txt"
    input_path.write_text("value", encoding="utf-8")
    module_path.write_text(
        """from pathlib import Path
from pydantic import BaseModel
from varve import Dependencies, Pipeline, stage

HELPER = 1

class Config(BaseModel):
    profile: str
    limit: int
    source: Path

class Demo(Pipeline):
    Config = Config
    @stage(
        produces="artifact.txt",
        depends=Dependencies(inputs={"source": lambda ctx: ctx.config.source}),
    )
    def build(self, ctx):
        (ctx.out / "artifact.txt").write_text("one", encoding="utf-8")
""",
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "review_error_demo")
    config = pipeline.Config(profile="profile", limit=1, source=input_path)
    output_base = tmp_path / "out"
    run(pipeline, config, cli_out=output_base)

    module_path.write_text(
        module_path.read_text(encoding="utf-8").replace("HELPER = 1", "HELPER = 2"),
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "review_error_demo")
    input_path.unlink()

    probe = probe_pipeline(
        pipeline,
        config,
        args=pipeline.Args(),
        out=output_base / "main",
    )[0]
    assert probe.decision.status == "error"
    assert probe.source_review == SourceReviewState("changed")
    with pytest.raises(ReviewRequiredError):
        run(pipeline, config, cli_out=output_base)

    with pytest.raises(ValueError, match="Cannot evaluate source review"):
        record_source_review(pipeline, config, decision="reuse", cli_out=output_base)
    assert Store(output_base / "main").read_review("build") is None
    pending = probe_pipeline(
        pipeline,
        config,
        args=pipeline.Args(),
        out=output_base / "main",
    )[0]
    assert pending.decision.status == "error"
    assert pending.source_review == SourceReviewState("changed")


def test_declared_source_symlink_is_an_evaluation_error(tmp_path: Path) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "helper-link.py").symlink_to(helper)
    module_path = tmp_path / "source_symlink_demo.py"
    source = _source('(ctx.out / "artifact.txt").write_text("same")')
    source = source.replace(
        "from varve import Pipeline, stage",
        "from varve import Dependencies, Pipeline, stage",
    ).replace(
        '@stage(produces="artifact.txt")',
        '@stage(produces="artifact.txt", depends=Dependencies(sources=[Path("helper-link.py")]))',
    )
    module_path.write_text(source, encoding="utf-8")
    pipeline = _load_pipeline(module_path, "source_symlink_demo")

    probe = probe_pipeline(
        pipeline,
        pipeline.Config(),
        args=pipeline.Args(),
        out=tmp_path / "out" / "main",
    )[0]

    assert probe.decision.status == "error"
    assert "Symlinks are not supported in source paths" in probe.decision.reason


def test_reused_source_uses_full_config_when_inputs_require_execution(
    tmp_path: Path,
) -> None:
    module_path = tmp_path / "config_demo.py"
    source_path = tmp_path / "input.txt"
    source_path.write_text("first", encoding="utf-8")
    module_path.write_text(
        """from pathlib import Path
from pydantic import BaseModel
from varve import Dependencies, Pipeline, stage

HELPER = 1

class Config(BaseModel):
    profile: str
    limit: int
    source: Path

class Demo(Pipeline):
    Config = Config
    @stage(
        produces="artifact.txt",
        depends=Dependencies(inputs={"source": lambda ctx: ctx.config.source}),
    )
    def build(self, ctx):
        (ctx.out / "artifact.txt").write_text(ctx.config.profile, encoding="utf-8")
""",
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "config_review_demo")
    config = pipeline.Config(profile="selected", limit=7, source=source_path)
    args = pipeline.Args()
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, config, args=args, cli_out=output_base)

    module_path.write_text(
        module_path.read_text(encoding="utf-8")
        .replace("HELPER = 1", "HELPER = 2")
        .replace(
            '(ctx.out / "artifact.txt").write_text(ctx.config.profile, encoding="utf-8")',
            '_ = ctx.config.profile\n        raise RuntimeError("stop after attempt")',
        ),
        encoding="utf-8",
    )
    # Keep the Stage body identical so only residual review source changes.
    module_path.write_text(
        """from pathlib import Path
from pydantic import BaseModel
from varve import Dependencies, Pipeline, stage

HELPER = 2

class Config(BaseModel):
    profile: str
    limit: int
    source: Path

class Demo(Pipeline):
    Config = Config
    @stage(
        produces="artifact.txt",
        depends=Dependencies(inputs={"source": lambda ctx: ctx.config.source}),
    )
    def build(self, ctx):
        (ctx.out / "artifact.txt").write_text(ctx.config.profile, encoding="utf-8")
""",
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "config_review_demo")
    config = pipeline.Config(profile="selected", limit=7, source=source_path)
    args = pipeline.Args()
    record_source_review(
        pipeline,
        config,
        args=args,
        decision="reuse",
        cli_out=output_base,
    )
    reused_before_input = probe_pipeline(
        pipeline,
        config,
        args=args,
        out=output_root,
    )[0]
    assert reused_before_input.source_review == SourceReviewState("changed", "reuse")
    assert reused_before_input.components is not None
    assert reused_before_input.components.config_access == ["profile"]
    assert reused_before_input.decision.status == "hit"

    source_path.write_text("second", encoding="utf-8")
    reused_probe = probe_pipeline(
        pipeline,
        config,
        args=args,
        out=output_root,
    )[0]
    # Input change forces a rerun, so Review is not required; the reuse decision remains stored.
    assert reused_probe.decision.status == "needs-run"
    assert Store(output_root).read_review("build") is not None

    module_path.write_text(
        """from pathlib import Path
from pydantic import BaseModel
from varve import Dependencies, Pipeline, stage

HELPER = 2

class Config(BaseModel):
    profile: str
    limit: int
    source: Path

class Demo(Pipeline):
    Config = Config
    @stage(
        produces="artifact.txt",
        depends=Dependencies(inputs={"source": lambda ctx: ctx.config.source}),
    )
    def build(self, ctx):
        _ = ctx.config.profile
        raise RuntimeError("stop after attempt")
""",
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "config_review_demo")
    with pytest.raises(RuntimeError, match="stop after attempt"):
        run(pipeline, config, args=args, cli_out=output_base)

    store = Store(output_root)
    attempt = store.read_attempt("build")
    failure = store.read_failure("build")
    assert attempt is not None
    assert failure is not None
    assert attempt.input_key == failure.input_key


def test_review_targets_validate_all_stages_before_writing(tmp_path: Path) -> None:
    (tmp_path / "one_source.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "two_source.py").write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "two_stage_demo.py"
    module_path.write_text(
        """from pathlib import Path
from pydantic import BaseModel
from varve import Dependencies, Pipeline, stage

class Config(BaseModel):
    pass

class Demo(Pipeline):
    Config = Config

    @stage(produces="one.txt", depends=Dependencies(review_sources=[Path("one_source.py")]))
    def one(self, ctx):
        (ctx.out / "one.txt").write_text("one", encoding="utf-8")

    @stage(produces="two.txt", depends=Dependencies(review_sources=[Path("two_source.py")]))
    def two(self, ctx):
        (ctx.out / "two.txt").write_text("two", encoding="utf-8")
""",
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "two_stage_review_demo")
    assert validate_base_stage_targets(pipeline.graph(), ("two", "one", "two")) == (
        "one",
        "two",
    )
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    (tmp_path / "one_source.py").write_text("VALUE = 2\n", encoding="utf-8")

    result = record_source_review(
        pipeline,
        pipeline.Config(),
        decision="reuse",
        targets=("one", "two"),
        cli_out=output_base,
    )

    store = Store(output_root)
    assert result.recorded == ("one",)
    assert result.did_not_need_review == ("two",)
    assert store.read_review("one") is not None
    assert store.read_review("two") is None


def test_review_can_correct_decision_for_same_fingerprint(tmp_path: Path) -> None:
    module_path = tmp_path / "decision_demo.py"
    module_path.write_text(_reviewable_source("1"), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "decision_review_demo")
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    module_path.write_text(_reviewable_source("2"), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "decision_review_demo")

    first = record_source_review(pipeline, pipeline.Config(), decision="reuse", cli_out=output_base)
    reused_at = Store(output_root).read_review("build").decided_at  # type: ignore[union-attr]
    repeated = record_source_review(
        pipeline, pipeline.Config(), decision="reuse", cli_out=output_base
    )
    assert first.recorded == ("build",)
    assert repeated.recorded == ()
    assert repeated.already_decided == ("build",)
    assert Store(output_root).read_review("build").decided_at == reused_at  # type: ignore[union-attr]
    assert probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)[
        0
    ].source_review == SourceReviewState("changed", "reuse")
    record_source_review(
        pipeline,
        pipeline.Config(),
        decision="invalidate",
        targets=("build",),
        cli_out=output_base,
    )
    assert probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)[
        0
    ].source_review == SourceReviewState("changed", "invalidate")
    assert Store(output_root).read_review("build").decided_at != reused_at  # type: ignore[union-attr]


def test_force_does_not_write_review_and_survives_failure(tmp_path: Path) -> None:
    module_path = tmp_path / "force_failure.py"
    module_path.write_text(
        """from pathlib import Path
from pydantic import BaseModel
from varve import Pipeline, stage

HELPER = 1

class Config(BaseModel):
    pass

class Args(BaseModel):
    fail: bool = False

class Demo(Pipeline):
    Config = Config
    Args = Args
    @stage(produces="artifact.txt")
    def build(self, ctx):
        if ctx.args.fail:
            raise RuntimeError("planned")
        (ctx.out / "artifact.txt").write_text("same", encoding="utf-8")
""",
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "force_failure_demo")
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), args=pipeline.Args(), cli_out=output_base)

    module_path.write_text(
        module_path.read_text(encoding="utf-8").replace("HELPER = 1", "HELPER = 2"),
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "force_failure_demo")
    record_source_review(
        pipeline, pipeline.Config(), args=pipeline.Args(), decision="reuse", cli_out=output_base
    )
    reused = Store(output_root).read_review("build")
    assert reused is not None

    with pytest.raises(RuntimeError, match="planned"):
        run(
            pipeline,
            pipeline.Config(),
            args=pipeline.Args(fail=True),
            cli_out=output_base,
            force=True,
        )
    # Force does not rewrite Stage ReviewRecords.
    assert Store(output_root).read_review("build") == reused
    assert Store(output_root).read_attempt("build") is not None

    with pytest.raises(RuntimeError, match="planned"):
        run(
            pipeline,
            pipeline.Config(),
            args=pipeline.Args(fail=True),
            cli_out=output_base,
            force=True,
        )
    assert Store(output_root).read_review("build") == reused


def test_normal_run_recovers_force_partial_from_full_config_key(tmp_path: Path) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "force_partial.py"
    module_path.write_text(_force_partial_source(), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "force_partial_demo")
    config = pipeline.Config(special="first", unused="unused")
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, config, args=pipeline.Args(), cli_out=output_base)
    (output_root / "calls.txt").unlink()
    helper.write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="planned"):
        run(
            pipeline,
            config,
            args=pipeline.Args(fail=True),
            cli_out=output_base,
            force=True,
        )

    store = Store(output_root)
    attempt = store.read_attempt("build")
    assert attempt is not None
    assert store.read_partial("build", attempt.input_key)
    assert store.read_review("build") is None

    run(pipeline, config, args=pipeline.Args(), cli_out=output_base)

    assert (output_root / "calls.txt").read_text(encoding="utf-8").splitlines() == [
        "0",
        "1",
        "2",
    ]
    assert store.read_attempt("build") is None
    assert store.read_review("build") is None
    success = store.read_success("build")
    assert success is not None
    assert success.key_components.config_access is None

    changed = probe_pipeline(
        pipeline,
        pipeline.Config(special="second", unused="unused"),
        args=pipeline.Args(),
        out=output_root,
    )[0]
    assert changed.decision.status == "needs-run"
    assert changed.decision.reason.startswith("config:")


def test_force_external_review_blocker_writes_nothing(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream.py"
    target = tmp_path / "target.py"
    upstream.write_text("VALUE = 1\n", encoding="utf-8")
    target.write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "external_review.py"
    module_path.write_text(
        """from pathlib import Path
from pydantic import BaseModel
from varve import Dependencies, Pipeline, stage

class Config(BaseModel):
    pass

class Demo(Pipeline):
    Config = Config

    @stage(produces="upstream.txt", depends=Dependencies(review_sources=[Path("upstream.py")]))
    def upstream(self, ctx):
        (ctx.out / "upstream.txt").write_text("upstream", encoding="utf-8")

    @stage(
        needs="upstream",
        produces="target.txt",
        depends=Dependencies(review_sources=[Path("target.py")]),
    )
    def target(self, ctx):
        (ctx.out / "target.txt").write_text("target", encoding="utf-8")
""",
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "external_review_demo")
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    upstream.write_text("VALUE = 2\n", encoding="utf-8")
    target.write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(ReviewRequiredError) as error:
        run(pipeline, pipeline.Config(), cli_out=output_base, only="target", force=True)

    assert error.value.stages == ["upstream"]
    assert Store(output_root).read_review("upstream") is None
    assert Store(output_root).read_review("target") is None


def test_force_external_pending_precedes_concurrent_evaluation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    upstream = tmp_path / "upstream.py"
    source = tmp_path / "input.txt"
    upstream.write_text("VALUE = 1\n", encoding="utf-8")
    source.write_text("value", encoding="utf-8")
    module_path = tmp_path / "external_pending_error.py"
    module_path.write_text(_external_pending_with_evaluation_error_source(), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "external_pending_error_demo")
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    config = pipeline.Config(source=source)
    run(pipeline, config, cli_out=output_base)
    artifact = output_root / "target.txt"
    before = artifact.read_bytes()
    upstream.write_text("VALUE = 2\n", encoding="utf-8")
    source.unlink()

    assert (
        pipeline.cli(
            [
                "run",
                "--only",
                "target",
                "--force",
                "--out",
                str(output_base),
            ]
        )
        == 2
    )

    store = Store(output_root)
    assert store.read_review("upstream") is None
    assert store.read_review("target") is None
    assert store.read_attempt("target") is None
    assert artifact.read_bytes() == before


def test_force_evaluation_error_writes_nothing(tmp_path: Path) -> None:
    module_path = tmp_path / "force_evaluation.py"
    source_path = tmp_path / "input.txt"
    source_path.write_text("value", encoding="utf-8")
    module_path.write_text(
        _source_with_input('(ctx.out / "artifact.txt").write_text("one")'),
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "force_evaluation_demo")
    config = pipeline.Config(profile="p", limit=1, source=source_path)
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, config, cli_out=output_base)
    module_path.write_text(
        _source_with_input('(ctx.out / "artifact.txt").write_text("two")'),
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "force_evaluation_demo")
    source_path.unlink()

    with pytest.raises(ValueError, match="Cannot evaluate selected stages"):
        run(pipeline, config, cli_out=output_base, force=True)
    assert Store(output_root).read_review("build") is None


def test_force_hard_interruption_preserves_attempt_without_review(tmp_path: Path) -> None:
    module_path = tmp_path / "force_interrupt.py"
    module_path.write_text(_reviewable_source("1"), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "force_interrupt_demo")
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    module_path.write_text(_forced_failure_source(hard_interrupt=True), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "force_interrupt_demo")

    with pytest.raises(KeyboardInterrupt):
        run(pipeline, pipeline.Config(), cli_out=output_base, force=True)

    store = Store(output_root)
    assert store.read_review("build") is None
    assert store.read_attempt("build") is not None


def test_force_failure_does_not_write_review_for_unstarted_selected_stage(
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared.py"
    shared.write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "force_unstarted.py"
    module_path.write_text(_forced_two_stage_source(), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "force_unstarted_demo")
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), args=pipeline.Args(), cli_out=output_base)
    shared.write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="planned first-stage failure"):
        run(
            pipeline,
            pipeline.Config(),
            args=pipeline.Args(fail=True),
            cli_out=output_base,
            force=True,
        )

    store = Store(output_root)
    assert store.read_review("one") is None
    assert store.read_review("two") is None
    assert store.read_review("three") is None
    assert store.read_attempt("two") is None
    assert store.read_attempt("three") is None

    # Pending Stage Review still blocks normal run after force failed mid-selection.
    with pytest.raises(ReviewRequiredError):
        run(pipeline, pipeline.Config(), args=pipeline.Args(), cli_out=output_base)
    record_source_review(pipeline, pipeline.Config(), decision="invalidate", cli_out=output_base)
    run(pipeline, pipeline.Config(), args=pipeline.Args(), cli_out=output_base)
    assert store.read_review("one") is not None
    assert store.read_review("two") is not None


def test_force_does_not_write_matrix_review_records(tmp_path: Path) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "force_matrix.py"
    module_path.write_text(_matrix_source(), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "force_matrix_demo")
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    helper.write_text("VALUE = 2\n", encoding="utf-8")
    run(pipeline, pipeline.Config(), cli_out=output_base, force=True)
    store = Store(output_root)
    assert store.read_review("build") is None
    assert store.read_review("build@cell=a") is None
    assert store.read_review("build@cell=b") is None


def test_matrix_review_is_base_stage_only(tmp_path: Path) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "matrix_demo.py"
    module_path.write_text(_matrix_source(), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "matrix_review_demo")
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    helper.write_text("VALUE = 2\n", encoding="utf-8")

    probes = probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)
    names = [probe.stage for probe in probes]
    assert names == ["build@cell=a", "build@cell=b"]
    with pytest.raises(ValueError, match="whole Stage"):
        record_source_review(
            pipeline,
            pipeline.Config(),
            decision="reuse",
            targets=(names[0],),
            cli_out=output_base,
        )
    assert Store(output_root).read_review("build") is None
    assert Store(output_root).read_review(names[0]) is None

    record_source_review(
        pipeline,
        pipeline.Config(),
        decision="invalidate",
        targets=("build",),
        cli_out=output_base,
    )
    assert Store(output_root).read_review("build") is not None
    reviews = {
        probe.stage: probe.source_review
        for probe in probe_pipeline(
            pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root
        )
    }
    assert reviews == {
        names[0]: SourceReviewState("changed", "invalidate"),
        names[1]: SourceReviewState("changed", "invalidate"),
    }


def test_coordinate_review_targets_fail_before_writes(tmp_path: Path) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "matrix_2d_demo.py"
    module_path.write_text(_matrix_2d_source(), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "matrix_2d_review_demo")
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    helper.write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="whole Stage"):
        record_source_review(
            pipeline,
            pipeline.Config(),
            decision="reuse",
            targets=("build@bench=a", "build@model=large"),
            cli_out=output_base,
        )
    assert Store(output_root).read_review("build") is None


def test_invalid_base_review_selector_writes_nothing(tmp_path: Path) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "invalid_selector_demo.py"
    module_path.write_text(_matrix_source(), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "invalid_selector_review_demo")
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    helper.write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="Unknown varve stage"):
        record_source_review(
            pipeline,
            pipeline.Config(),
            decision="reuse",
            targets=("build", "missing"),
            cli_out=output_base,
        )

    assert Store(output_root).read_review("build") is None
    assert Store(output_root).read_review("build@cell=a") is None
    assert Store(output_root).read_review("build@cell=b") is None


def test_top_level_and_bulk_review_routes_record_without_running(tmp_path: Path) -> None:
    def source(helper: str) -> str:
        return _reviewable_source(helper).replace(
            '(ctx.out / "artifact.txt").write_text("same", encoding="utf-8")',
            'with (ctx.out / "calls.txt").open("a", encoding="utf-8") as stream:\n'
            '            stream.write("called\\n")\n'
            '        (ctx.out / "artifact.txt").write_text("same", encoding="utf-8")',
        )

    module_path = tmp_path / "dashboard_demo.py"
    module_path.write_text(source("1"), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "dashboard_review_demo")
    output_base = tmp_path / "demo" / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    calls = output_root / "calls.txt"
    calls.unlink()
    artifact = output_root / "artifact.txt"
    before = artifact.read_bytes()
    store = Store(output_root)
    success = store.read_success("build")
    module_path.write_text(source("2"), encoding="utf-8")
    _load_pipeline(module_path, "dashboard_review_demo")

    assert dashboard_main(["reuse", "dashboard_review_demo", "--root", str(tmp_path)]) == 0

    review = store.read_review("build")
    assert review is not None
    assert review.decision == "reuse"
    assert not calls.exists()
    assert artifact.read_bytes() == before
    assert store.read_success("build") == success
    assert store.read_attempt("build") is None
    assert store.read_failure("build") is None

    module_path.write_text(source("3"), encoding="utf-8")
    _load_pipeline(module_path, "dashboard_review_demo")
    assert dashboard_main(["invalidate", "--all", "--root", str(tmp_path)]) == 0

    review = store.read_review("build")
    assert review is not None
    assert review.decision == "invalidate"
    assert not calls.exists()
    assert artifact.read_bytes() == before
    assert store.read_success("build") == success
    assert store.read_attempt("build") is None
    assert store.read_failure("build") is None


def test_reuse_preserves_batch_partial_and_invalidate_restarts_from_zero(
    tmp_path: Path,
) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "batch_demo.py"
    module_path.write_text(_batch_source(), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "batch_review_demo")

    reused_base = tmp_path / "reuse"
    reused_input = tmp_path / "reused-input.txt"
    reused_input.write_text("first", encoding="utf-8")
    run(
        pipeline,
        pipeline.Config(input=reused_input),
        args=pipeline.Args(),
        cli_out=reused_base,
    )
    (reused_base / "main" / "calls.txt").unlink()
    reused_input.write_text("second", encoding="utf-8")
    with pytest.raises(RuntimeError, match="planned failure"):
        run(
            pipeline,
            pipeline.Config(input=reused_input),
            args=pipeline.Args(fail_after=1),
            cli_out=reused_base,
        )
    helper.write_text("VALUE = 2\n", encoding="utf-8")
    record_source_review(
        pipeline,
        pipeline.Config(input=reused_input),
        args=pipeline.Args(),
        decision="reuse",
        cli_out=reused_base,
    )
    reused_probe = probe_pipeline(
        pipeline,
        pipeline.Config(input=reused_input),
        args=pipeline.Args(),
        out=reused_base / "main",
    )[0]
    assert reused_probe.decision.status == "failed"
    assert reused_probe.decision.display_reason == "stage-failed · resume 2/3"
    run(
        pipeline,
        pipeline.Config(input=reused_input),
        args=pipeline.Args(),
        cli_out=reused_base,
    )
    assert (reused_base / "main" / "calls.txt").read_text(encoding="utf-8").splitlines() == [
        "0",
        "1",
        "2",
    ]

    invalidated_base = tmp_path / "invalidate"
    invalidated_input = tmp_path / "invalidated-input.txt"
    invalidated_input.write_text("first", encoding="utf-8")
    run(
        pipeline,
        pipeline.Config(input=invalidated_input),
        args=pipeline.Args(),
        cli_out=invalidated_base,
    )
    (invalidated_base / "main" / "calls.txt").unlink()
    invalidated_input.write_text("second", encoding="utf-8")
    with pytest.raises(RuntimeError, match="planned failure"):
        run(
            pipeline,
            pipeline.Config(input=invalidated_input),
            args=pipeline.Args(fail_after=1),
            cli_out=invalidated_base,
        )
    helper.write_text("VALUE = 3\n", encoding="utf-8")
    record_source_review(
        pipeline,
        pipeline.Config(input=invalidated_input),
        args=pipeline.Args(),
        decision="invalidate",
        cli_out=invalidated_base,
    )
    invalidated_probe = probe_pipeline(
        pipeline,
        pipeline.Config(input=invalidated_input),
        args=pipeline.Args(),
        out=invalidated_base / "main",
    )[0]
    assert invalidated_probe.decision.status == "failed"
    assert invalidated_probe.source_review == SourceReviewState("changed", "invalidate")
    run(
        pipeline,
        pipeline.Config(input=invalidated_input),
        args=pipeline.Args(),
        cli_out=invalidated_base,
    )
    assert (invalidated_base / "main" / "calls.txt").read_text(encoding="utf-8").splitlines() == [
        "0",
        "1",
        "0",
        "1",
        "2",
    ]
