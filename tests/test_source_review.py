from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from varve.dashboard.cli import main as dashboard_main
from varve.engine import runner as runner_module
from varve.engine.runner import (
    ReviewRequiredError,
    _KeyingSession,
    probe_pipeline,
    record_source_review,
    run,
)
from varve.engine.state import SourceReviewState
from varve.keying import source as source_module
from varve.models import SourceManifestEntry
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

    @stage(produces="upstream.txt", depends=Dependencies(sources=[Path("upstream.py")]))
    def upstream(self, ctx):
        (ctx.out / "upstream.txt").write_text("upstream", encoding="utf-8")

    @stage(
        needs="upstream",
        produces="target.txt",
        depends=Dependencies(sources=[Path("target.py")]),
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

    @stage(produces="upstream.txt", depends=Dependencies(sources=[Path("upstream.py")]))
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
    return _source(exception)


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

    @stage(produces="one.txt", depends=Dependencies(sources=[Path("shared.py")]))
    def one(self, ctx):
        if ctx.args.fail:
            raise RuntimeError("planned first-stage failure")
        (ctx.out / "one.txt").write_text("one", encoding="utf-8")

    @stage(needs="one", produces="two.txt", depends=Dependencies(sources=[Path("shared.py")]))
    def two(self, ctx):
        (ctx.out / "two.txt").write_text("two", encoding="utf-8")

    @stage(needs="two", produces="three.txt")
    def three(self, ctx):
        (ctx.out / "three.txt").write_text("three", encoding="utf-8")
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
    @stage(produces="artifact.txt", depends=Dependencies(sources=[Path("helper.py")]))
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
    @stage(produces="artifact.txt", depends=Dependencies(sources=[Path("helper.py")]))
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
            sources=[Path("helper.py")],
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


def test_source_change_requires_review_and_accept_does_not_rewrite_success(tmp_path: Path) -> None:
    module_path = tmp_path / "demo.py"
    module_path.write_text(_source('(ctx.out / "artifact.txt").write_text("same")'))
    pipeline = _load_pipeline(module_path, "review_demo")
    run(pipeline, pipeline.Config(), cli_out=tmp_path / "out")
    output_root = tmp_path / "out" / "main"
    before = (output_root / ".varve" / "stages" / "build.json").read_bytes()

    module_path.write_text(
        _source('value = "same"\n        (ctx.out / "artifact.txt").write_text(value)')
    )
    pipeline = _load_pipeline(module_path, "review_demo")
    probe = probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)[0]
    assert probe.source_review == SourceReviewState("changed")
    with pytest.raises(ReviewRequiredError) as error:
        run(pipeline, pipeline.Config(), cli_out=tmp_path / "out")
    assert error.value.stages == ["build"]
    assert str(error.value) == ("Source review required for: build. Run accept or reject first.")
    assert pipeline.cli(["run", "--out", str(tmp_path / "out")]) == 2

    assert pipeline.cli(["accept", "--out", str(tmp_path / "out")]) == 0
    assert (output_root / ".varve" / "stages" / "build.json").read_bytes() == before
    assert probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)[
        0
    ].source_review == SourceReviewState("changed", "accept")
    module_path.write_text(
        _source(
            'value = "same"\n        marker = 1\n        (ctx.out / "artifact.txt").write_text(value)'
        )
    )
    pipeline = _load_pipeline(module_path, "review_demo")
    assert probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)[
        0
    ].source_review == SourceReviewState("changed")
    record_source_review(
        pipeline,
        pipeline.Config(),
        decision="reject",
        cli_out=tmp_path / "out",
    )
    rejected = probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)[0]
    assert rejected.source_review == SourceReviewState("changed", "reject")
    assert rejected.decision.status == "hit"
    assert rejected.decision.reason == "hit"
    run(pipeline, pipeline.Config(), cli_out=tmp_path / "out")
    assert Store(output_root).read_review("build") is None


def test_comment_only_source_change_keeps_fingerprint(tmp_path: Path) -> None:
    module_path = tmp_path / "demo.py"
    module_path.write_text(_source('(ctx.out / "artifact.txt").write_text("same")'))
    pipeline = _load_pipeline(module_path, "comment_demo")
    first = probe_pipeline(
        pipeline, pipeline.Config(), args=pipeline.Args(), out=tmp_path / "out" / "main"
    )[0].source_fingerprint.fingerprint
    module_path.write_text(module_path.read_text() + "\n# formatting-only comment\n")
    pipeline = _load_pipeline(module_path, "comment_demo")
    second = probe_pipeline(
        pipeline, pipeline.Config(), args=pipeline.Args(), out=tmp_path / "out" / "main"
    )[0].source_fingerprint.fingerprint
    assert second == first


def test_pipeline_review_without_source_changes_is_a_successful_noop(tmp_path: Path) -> None:
    module_path = tmp_path / "no_review.py"
    module_path.write_text(_source('(ctx.out / "artifact.txt").write_text("same")'))
    pipeline = _load_pipeline(module_path, "no_review_demo")

    result = record_source_review(
        pipeline,
        pipeline.Config(),
        decision="accept",
        cli_out=tmp_path / "out",
    )

    assert result.matched_cells == ()
    assert result.source_changed_cells == ()
    assert result.recorded == ()
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
        decision="accept",
        cli_out=tmp_path / "out",
        _keying_session=session,
    )

    assert observed_sessions == [session]


def test_docstring_change_opens_source_review(tmp_path: Path) -> None:
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

    assert probe.source_review == SourceReviewState("changed")


def test_dependency_error_preserves_pending_source_review(tmp_path: Path) -> None:
    module_path = tmp_path / "review_error_demo.py"
    input_path = tmp_path / "input.txt"
    input_path.write_text("value", encoding="utf-8")
    module_path.write_text(
        _source_with_input('(ctx.out / "artifact.txt").write_text("one", encoding="utf-8")'),
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "review_error_demo")
    config = pipeline.Config(profile="profile", limit=1, source=input_path)
    output_base = tmp_path / "out"
    run(pipeline, config, cli_out=output_base)

    module_path.write_text(
        _source_with_input('(ctx.out / "artifact.txt").write_text("two", encoding="utf-8")'),
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

    record_source_review(pipeline, config, decision="accept", cli_out=output_base)
    accepted = probe_pipeline(
        pipeline,
        config,
        args=pipeline.Args(),
        out=output_base / "main",
    )[0]
    assert accepted.decision.status == "error"
    assert accepted.source_review == SourceReviewState("changed", "accept")


def test_source_parser_honors_pep_263_encoding(tmp_path: Path) -> None:
    module_path = tmp_path / "latin1_demo.py"
    source = "# coding: latin-1\n" + _source(
        '(ctx.out / "artifact.txt").write_text("é", encoding="utf-8")'
    )
    module_path.write_bytes(source.encode("latin-1"))
    pipeline = _load_pipeline(module_path, "latin1_demo")

    probe = probe_pipeline(
        pipeline,
        pipeline.Config(),
        args=pipeline.Args(),
        out=tmp_path / "out" / "main",
    )[0]

    assert probe.decision.status == "needs-run"
    assert probe.source_fingerprint.fingerprint != "error"


def test_source_stat_cache_requires_same_physical_path(tmp_path: Path) -> None:
    source_path = tmp_path / "source.py"
    source_path.write_text("VALUE = 2\n", encoding="utf-8")
    stat = source_path.stat()
    stale = SourceManifestEntry(
        path="pipeline/source.py",
        cache_path=str(tmp_path / "other-checkout" / "source.py"),
        digest="sha256:stale",
        inode=stat.st_ino,
        size=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
    )

    observed = source_module._source_entry(
        "pipeline/source.py",
        source_path,
        stale,
        force_rehash=False,
    )

    assert observed.cache_path == str(source_path.resolve())
    assert observed.digest != stale.digest


def test_source_stat_cache_is_reused_and_force_rehash_bypasses_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_path = tmp_path / "cached_source_demo.py"
    module_path.write_text(_source('(ctx.out / "artifact.txt").write_text("same")'))
    pipeline = _load_pipeline(module_path, "cached_source_demo")
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    original_parse = source_module.ast.parse
    calls = 0

    def counted_parse(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(source_module.ast, "parse", counted_parse)

    cached = probe_pipeline(
        pipeline,
        pipeline.Config(),
        args=pipeline.Args(),
        out=output_root,
    )[0]
    assert cached.decision.status == "hit"
    assert calls == 0

    rehashed = probe_pipeline(
        pipeline,
        pipeline.Config(),
        args=pipeline.Args(),
        out=output_root,
        force_rehash=True,
    )[0]
    assert rehashed.decision.status == "hit"
    assert calls > 0


def test_declared_source_directory_tracks_added_python_files(tmp_path: Path) -> None:
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    (helpers / "first.py").write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "declared_demo.py"
    source = _source('(ctx.out / "artifact.txt").write_text("same")')
    source = source.replace(
        "from varve import Pipeline, stage",
        "from varve import Dependencies, Pipeline, stage",
    ).replace(
        '@stage(produces="artifact.txt")',
        '@stage(produces="artifact.txt", depends=Dependencies(sources=[Path("helpers")]))',
    )
    module_path.write_text(source, encoding="utf-8")
    pipeline = _load_pipeline(module_path, "declared_demo")
    output_base = tmp_path / "out"
    run(pipeline, pipeline.Config(), cli_out=output_base)

    (helpers / "second.py").write_text("VALUE = 2\n", encoding="utf-8")
    pipeline = _load_pipeline(module_path, "declared_demo")
    probe = probe_pipeline(
        pipeline,
        pipeline.Config(),
        args=pipeline.Args(),
        out=output_base / "main",
    )[0]
    assert probe.source_review == SourceReviewState("changed")
    assert any(item.path.endswith("second.py") for item in probe.source_fingerprint.files)
    added_fingerprint = probe.source_fingerprint.fingerprint

    (helpers / "first.py").unlink()
    removed = probe_pipeline(
        pipeline,
        pipeline.Config(),
        args=pipeline.Args(),
        out=output_base / "main",
    )[0]
    assert removed.source_fingerprint.fingerprint != added_fingerprint

    (helpers / "second.py").rename(helpers / "renamed.py")
    renamed = probe_pipeline(
        pipeline,
        pipeline.Config(),
        args=pipeline.Args(),
        out=output_base / "main",
    )[0]
    assert renamed.source_fingerprint.fingerprint != removed.source_fingerprint.fingerprint


@pytest.mark.parametrize(
    ("declared_name", "contents", "message"),
    [
        ("missing.py", None, "Declared source path does not exist"),
        ("helper.txt", "value\n", "Declared source file must end in .py"),
        ("broken.py", "def broken(:\n", "Cannot parse Python source file"),
    ],
)
def test_invalid_declared_source_is_an_evaluation_error(
    tmp_path: Path,
    declared_name: str,
    contents: str | None,
    message: str,
) -> None:
    if contents is not None:
        (tmp_path / declared_name).write_text(contents, encoding="utf-8")
    module_path = tmp_path / "invalid_source_demo.py"
    source = _source('(ctx.out / "artifact.txt").write_text("same")')
    source = source.replace(
        "from varve import Pipeline, stage",
        "from varve import Dependencies, Pipeline, stage",
    ).replace(
        '@stage(produces="artifact.txt")',
        f'@stage(produces="artifact.txt", depends=Dependencies(sources=[Path("{declared_name}")]))',
    )
    module_path.write_text(source, encoding="utf-8")
    pipeline = _load_pipeline(module_path, f"invalid_source_{declared_name.replace('.', '_')}")

    probe = probe_pipeline(
        pipeline,
        pipeline.Config(),
        args=pipeline.Args(),
        out=tmp_path / "out" / "main",
    )[0]

    assert probe.decision.status == "error"
    assert message in probe.decision.reason


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


def test_accepted_source_uses_full_config_when_inputs_require_execution(
    tmp_path: Path,
) -> None:
    module_path = tmp_path / "config_demo.py"
    source_path = tmp_path / "input.txt"
    source_path.write_text("first", encoding="utf-8")
    module_path.write_text(
        _source_with_input(
            '(ctx.out / "artifact.txt").write_text(ctx.config.profile, encoding="utf-8")'
        ),
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "config_review_demo")
    config = pipeline.Config(profile="selected", limit=7, source=source_path)
    args = pipeline.Args()
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, config, args=args, cli_out=output_base)

    module_path.write_text(
        _source_with_input(
            '_ = ctx.config.profile\n        raise RuntimeError("stop after attempt")'
        ),
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "config_review_demo")
    config = pipeline.Config(profile="selected", limit=7, source=source_path)
    args = pipeline.Args()
    record_source_review(
        pipeline,
        config,
        args=args,
        decision="accept",
        cli_out=output_base,
    )
    source_path.write_text("second", encoding="utf-8")
    accepted_probe = probe_pipeline(
        pipeline,
        config,
        args=args,
        out=output_root,
    )[0]
    assert accepted_probe.source_review == SourceReviewState("changed", "accept")
    assert accepted_probe.components is not None
    assert accepted_probe.components.config_access == ["profile"]
    assert accepted_probe.decision.status == "needs-run"

    with pytest.raises(RuntimeError, match="stop after attempt"):
        run(pipeline, config, args=args, cli_out=output_base)

    store = Store(output_root)
    attempt = store.read_attempt("build")
    failure = store.read_failure("build")
    assert attempt is not None
    assert failure is not None
    assert attempt.input_key == failure.input_key
    assert attempt.input_key != accepted_probe.decision_key


def test_review_targets_validate_all_stages_before_writing(tmp_path: Path) -> None:
    (tmp_path / "one_source.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "two_source.py").write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "two_stage_demo.py"
    module_path.write_text(_two_stage_source(), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "two_stage_review_demo")
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    (tmp_path / "one_source.py").write_text("VALUE = 2\n", encoding="utf-8")

    result = record_source_review(
        pipeline,
        pipeline.Config(),
        decision="accept",
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
    module_path.write_text(_source('(ctx.out / "artifact.txt").write_text("one")'))
    pipeline = _load_pipeline(module_path, "decision_review_demo")
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    module_path.write_text(_source('(ctx.out / "artifact.txt").write_text("two")'))
    pipeline = _load_pipeline(module_path, "decision_review_demo")

    first = record_source_review(
        pipeline, pipeline.Config(), decision="accept", cli_out=output_base
    )
    accepted_at = Store(output_root).read_review("build").decided_at  # type: ignore[union-attr]
    repeated = record_source_review(
        pipeline, pipeline.Config(), decision="accept", cli_out=output_base
    )
    assert first.recorded == ("build",)
    assert repeated.recorded == ()
    assert repeated.already_decided == ("build",)
    assert Store(output_root).read_review("build").decided_at == accepted_at  # type: ignore[union-attr]
    assert probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)[
        0
    ].source_review == SourceReviewState("changed", "accept")
    record_source_review(
        pipeline,
        pipeline.Config(),
        decision="reject",
        targets=("build",),
        cli_out=output_base,
    )
    assert probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)[
        0
    ].source_review == SourceReviewState("changed", "reject")
    assert Store(output_root).read_review("build").decided_at != accepted_at  # type: ignore[union-attr]


def test_force_auto_reject_is_idempotent_and_survives_failure(tmp_path: Path) -> None:
    module_path = tmp_path / "force_failure.py"
    module_path.write_text(_source('(ctx.out / "artifact.txt").write_text("one")'))
    pipeline = _load_pipeline(module_path, "force_failure_demo")
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), cli_out=output_base)

    module_path.write_text(_forced_failure_source())
    pipeline = _load_pipeline(module_path, "force_failure_demo")
    record_source_review(pipeline, pipeline.Config(), decision="accept", cli_out=output_base)
    accepted = Store(output_root).read_review("build")
    assert accepted is not None

    with pytest.raises(RuntimeError, match="planned"):
        run(pipeline, pipeline.Config(), cli_out=output_base, force=True)
    rejected = Store(output_root).read_review("build")
    assert rejected is not None
    assert rejected.decision == "reject"
    assert rejected.decided_at != accepted.decided_at

    with pytest.raises(RuntimeError, match="planned"):
        run(pipeline, pipeline.Config(), cli_out=output_base, force=True)
    repeated = Store(output_root).read_review("build")
    assert repeated is not None
    assert repeated.decided_at == rejected.decided_at
    assert probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)[
        0
    ].source_review == SourceReviewState("changed", "reject")


def test_force_external_review_blocker_writes_nothing(tmp_path: Path) -> None:
    upstream = tmp_path / "upstream.py"
    target = tmp_path / "target.py"
    upstream.write_text("VALUE = 1\n", encoding="utf-8")
    target.write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "external_review.py"
    module_path.write_text(_dependent_two_stage_source(), encoding="utf-8")
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


def test_force_hard_interruption_preserves_reject_and_attempt(tmp_path: Path) -> None:
    module_path = tmp_path / "force_interrupt.py"
    module_path.write_text(_source('(ctx.out / "artifact.txt").write_text("one")'))
    pipeline = _load_pipeline(module_path, "force_interrupt_demo")
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    module_path.write_text(_forced_failure_source(hard_interrupt=True))
    pipeline = _load_pipeline(module_path, "force_interrupt_demo")

    with pytest.raises(KeyboardInterrupt):
        run(pipeline, pipeline.Config(), cli_out=output_base, force=True)

    store = Store(output_root)
    review = store.read_review("build")
    assert review is not None and review.decision == "reject"
    assert store.read_attempt("build") is not None


def test_force_failure_preserves_reject_for_unstarted_selected_stage(tmp_path: Path) -> None:
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
    assert store.read_review("one") is not None
    assert store.read_review("two") is not None
    assert store.read_review("three") is None
    assert store.read_attempt("two") is None
    assert store.read_attempt("three") is None

    run(pipeline, pipeline.Config(), args=pipeline.Args(), cli_out=output_base)
    assert store.read_review("one") is None
    assert store.read_review("two") is None
    assert store.read_review("three") is None


def test_force_review_write_failure_is_fail_closed_and_repeatable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "force_write_failure.py"
    module_path.write_text(_matrix_source(), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "force_write_failure_demo")
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    artifacts = [
        output_root / ".matrix" / "build" / f"cell={cell}" / "artifact.txt" for cell in ("a", "b")
    ]
    before = [path.stat().st_mtime_ns for path in artifacts]
    helper.write_text("VALUE = 2\n", encoding="utf-8")
    original = Store.write_review
    calls = 0

    def fail_second(store: Store, stage: str, record) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("planned review write failure")
        original(store, stage, record)

    monkeypatch.setattr(Store, "write_review", fail_second)
    with pytest.raises(OSError, match="planned review write failure"):
        run(pipeline, pipeline.Config(), cli_out=output_base, force=True)

    store = Store(output_root)
    assert store.read_review("build@cell=a") is not None
    assert store.read_review("build@cell=b") is None
    assert [path.stat().st_mtime_ns for path in artifacts] == before

    run(pipeline, pipeline.Config(), cli_out=output_base, force=True)
    assert store.read_review("build@cell=a") is None
    assert store.read_review("build@cell=b") is None


def test_matrix_base_and_concrete_review_targets_have_expected_scope(tmp_path: Path) -> None:
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
    record_source_review(
        pipeline,
        pipeline.Config(),
        decision="accept",
        targets=(names[0],),
        cli_out=output_base,
    )
    reviews = {
        probe.stage: probe.source_review
        for probe in probe_pipeline(
            pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root
        )
    }
    assert reviews == {
        names[0]: SourceReviewState("changed", "accept"),
        names[1]: SourceReviewState("changed"),
    }

    record_source_review(
        pipeline,
        pipeline.Config(),
        decision="reject",
        targets=("build",),
        cli_out=output_base,
    )
    reviews = {
        probe.stage: probe.source_review
        for probe in probe_pipeline(
            pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root
        )
    }
    assert reviews == {
        names[0]: SourceReviewState("changed", "reject"),
        names[1]: SourceReviewState("changed", "reject"),
    }


def test_review_repeatable_partial_selectors_union_and_skip_current_cells(tmp_path: Path) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "matrix_2d_demo.py"
    module_path.write_text(_matrix_2d_source(), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "matrix_2d_review_demo")
    output_base = tmp_path / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    helper.write_text("VALUE = 2\n", encoding="utf-8")
    run(
        pipeline,
        pipeline.Config(),
        cli_out=output_base,
        only="build@bench=a,model=small",
        force=True,
    )

    result = record_source_review(
        pipeline,
        pipeline.Config(),
        decision="accept",
        targets=("build@bench=a", "build@model=large"),
        cli_out=output_base,
    )

    assert result.matched_cells == (
        "build@bench=a,model=small",
        "build@bench=a,model=large",
        "build@bench=b,model=large",
    )
    assert result.recorded == (
        "build@bench=a,model=large",
        "build@bench=b,model=large",
    )
    assert result.did_not_need_review == ("build@bench=a,model=small",)
    assert Store(output_root).read_review("build@bench=b,model=small") is None


def test_invalid_repeatable_review_selector_writes_nothing(tmp_path: Path) -> None:
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
            decision="accept",
            targets=("build@cell=a", "missing"),
            cli_out=output_base,
        )

    assert Store(output_root).read_review("build@cell=a") is None
    assert Store(output_root).read_review("build@cell=b") is None


def test_dashboard_accept_route_records_review_without_running(tmp_path: Path) -> None:
    module_path = tmp_path / "dashboard_demo.py"
    module_path.write_text(_source('(ctx.out / "artifact.txt").write_text("one")'))
    pipeline = _load_pipeline(module_path, "dashboard_review_demo")
    output_base = tmp_path / "demo" / "out"
    output_root = output_base / "main"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    artifact = output_root / "artifact.txt"
    before = artifact.read_bytes()
    module_path.write_text(_source('(ctx.out / "artifact.txt").write_text("two")'))
    _load_pipeline(module_path, "dashboard_review_demo")

    assert dashboard_main(["accept", "dashboard_review_demo", "--root", str(tmp_path)]) == 0

    review = Store(output_root).read_review("build")
    assert review is not None
    assert review.decision == "accept"
    assert artifact.read_bytes() == before


def test_accept_preserves_batch_partial_and_reject_restarts_from_zero(
    tmp_path: Path,
) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "batch_demo.py"
    module_path.write_text(_batch_source(), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "batch_review_demo")

    accepted_base = tmp_path / "accepted"
    accepted_input = tmp_path / "accepted-input.txt"
    accepted_input.write_text("first", encoding="utf-8")
    run(
        pipeline,
        pipeline.Config(input=accepted_input),
        args=pipeline.Args(),
        cli_out=accepted_base,
    )
    (accepted_base / "main" / "calls.txt").unlink()
    accepted_input.write_text("second", encoding="utf-8")
    with pytest.raises(RuntimeError, match="planned failure"):
        run(
            pipeline,
            pipeline.Config(input=accepted_input),
            args=pipeline.Args(fail_after=1),
            cli_out=accepted_base,
        )
    helper.write_text("VALUE = 2\n", encoding="utf-8")
    record_source_review(
        pipeline,
        pipeline.Config(input=accepted_input),
        args=pipeline.Args(),
        decision="accept",
        cli_out=accepted_base,
    )
    accepted_probe = probe_pipeline(
        pipeline,
        pipeline.Config(input=accepted_input),
        args=pipeline.Args(),
        out=accepted_base / "main",
    )[0]
    assert accepted_probe.decision.status == "failed"
    assert accepted_probe.decision.display_reason == "stage-failed · resume 2/3"
    run(
        pipeline,
        pipeline.Config(input=accepted_input),
        args=pipeline.Args(),
        cli_out=accepted_base,
    )
    assert (accepted_base / "main" / "calls.txt").read_text(encoding="utf-8").splitlines() == [
        "0",
        "1",
        "2",
    ]

    rejected_base = tmp_path / "rejected"
    rejected_input = tmp_path / "rejected-input.txt"
    rejected_input.write_text("first", encoding="utf-8")
    run(
        pipeline,
        pipeline.Config(input=rejected_input),
        args=pipeline.Args(),
        cli_out=rejected_base,
    )
    (rejected_base / "main" / "calls.txt").unlink()
    rejected_input.write_text("second", encoding="utf-8")
    with pytest.raises(RuntimeError, match="planned failure"):
        run(
            pipeline,
            pipeline.Config(input=rejected_input),
            args=pipeline.Args(fail_after=1),
            cli_out=rejected_base,
        )
    helper.write_text("VALUE = 3\n", encoding="utf-8")
    record_source_review(
        pipeline,
        pipeline.Config(input=rejected_input),
        args=pipeline.Args(),
        decision="reject",
        cli_out=rejected_base,
    )
    rejected_probe = probe_pipeline(
        pipeline,
        pipeline.Config(input=rejected_input),
        args=pipeline.Args(),
        out=rejected_base / "main",
    )[0]
    assert rejected_probe.decision.status == "failed"
    assert rejected_probe.source_review == SourceReviewState("changed", "reject")
    run(
        pipeline,
        pipeline.Config(input=rejected_input),
        args=pipeline.Args(),
        cli_out=rejected_base,
    )
    assert (rejected_base / "main" / "calls.txt").read_text(encoding="utf-8").splitlines() == [
        "0",
        "1",
        "0",
        "1",
        "2",
    ]
