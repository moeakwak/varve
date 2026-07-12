from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from varve.dashboard.cli import main as dashboard_main
from varve.engine.runner import ReviewRequiredError, probe_pipeline, record_source_review, run
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
    assert probe.source_review == "pending"
    with pytest.raises(ReviewRequiredError):
        run(pipeline, pipeline.Config(), cli_out=tmp_path / "out")

    assert pipeline.cli(["accept", "--out", str(tmp_path / "out")]) == 0
    assert (output_root / ".varve" / "stages" / "build.json").read_bytes() == before
    assert (
        probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)[
            0
        ].source_review
        == "accepted"
    )
    module_path.write_text(
        _source(
            'value = "same"\n        marker = 1\n        (ctx.out / "artifact.txt").write_text(value)'
        )
    )
    pipeline = _load_pipeline(module_path, "review_demo")
    assert (
        probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)[
            0
        ].source_review
        == "pending"
    )
    record_source_review(
        pipeline,
        pipeline.Config(),
        decision="reject",
        cli_out=tmp_path / "out",
    )
    rejected = probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)[0]
    assert rejected.source_review == "rerun-required"
    assert rejected.decision.status == "needs-run"
    assert rejected.decision.reason == "source-change"
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

    assert probe.source_review == "pending"


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
    assert probe.source_review == "pending"

    record_source_review(pipeline, config, decision="accept", cli_out=output_base)
    accepted = probe_pipeline(
        pipeline,
        config,
        args=pipeline.Args(),
        out=output_base / "main",
    )[0]
    assert accepted.decision.status == "error"
    assert accepted.source_review == "accepted"


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
    assert probe.source_review == "pending"
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
    assert accepted_probe.source_review == "accepted"
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

    with pytest.raises(ValueError, match="Stage has no source changes: two"):
        record_source_review(
            pipeline,
            pipeline.Config(),
            decision="accept",
            targets=("one", "two"),
            cli_out=output_base,
        )

    store = Store(output_root)
    assert store.read_review("one") is None
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

    record_source_review(pipeline, pipeline.Config(), decision="accept", cli_out=output_base)
    assert (
        probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)[
            0
        ].source_review
        == "accepted"
    )
    record_source_review(
        pipeline,
        pipeline.Config(),
        decision="reject",
        targets=("build",),
        cli_out=output_base,
    )
    assert (
        probe_pipeline(pipeline, pipeline.Config(), args=pipeline.Args(), out=output_root)[
            0
        ].source_review
        == "rerun-required"
    )


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
    assert reviews == {names[0]: "accepted", names[1]: "pending"}

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
    assert reviews == {names[0]: "rerun-required", names[1]: "rerun-required"}


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

    assert dashboard_main(["accept", "demo", "--root", str(tmp_path)]) == 0

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
    assert rejected_probe.decision.status == "needs-run"
    assert rejected_probe.decision.reason == "source-change"
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
