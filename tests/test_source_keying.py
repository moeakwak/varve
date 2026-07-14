from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from varve.engine.runner import probe_pipeline, run
from varve.engine.state import SourceReviewState
from varve.keying import source as source_module
from varve.keying.source import SourceFingerprintSession
from varve.models import SourceManifestEntry


def _load_pipeline(path: Path, module_name: str):
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.Demo


def _two_stage_shared_helper_source() -> str:
    return """from pathlib import Path
from pydantic import BaseModel
from varve import Pipeline, stage

HELPER = 1

class Config(BaseModel):
    pass

class Demo(Pipeline):
    Config = Config

    @stage(produces="one.txt")
    def one(self, ctx):
        (ctx.out / "one.txt").write_text(str(HELPER), encoding="utf-8")

    @stage(produces="two.txt")
    def two(self, ctx):
        (ctx.out / "two.txt").write_text("two", encoding="utf-8")
"""


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


def test_comment_only_source_change_keeps_fingerprint(tmp_path: Path) -> None:
    module_path = tmp_path / "demo.py"
    module_path.write_text(_source('(ctx.out / "artifact.txt").write_text("same")'))
    pipeline = _load_pipeline(module_path, "comment_keying_demo")
    first = probe_pipeline(
        pipeline, pipeline.Config(), args=pipeline.Args(), out=tmp_path / "out" / "main"
    )[0]
    module_path.write_text(module_path.read_text() + "\n# formatting-only comment\n")
    pipeline = _load_pipeline(module_path, "comment_keying_demo")
    second = probe_pipeline(
        pipeline, pipeline.Config(), args=pipeline.Args(), out=tmp_path / "out" / "main"
    )[0]
    assert second.source_observation.rerun.fingerprint == first.source_observation.rerun.fingerprint
    assert (
        second.source_observation.review.fingerprint == first.source_observation.review.fingerprint
    )


def test_stage_body_change_only_affects_that_stage_rerun(tmp_path: Path) -> None:
    module_path = tmp_path / "two_stage.py"
    module_path.write_text(_two_stage_shared_helper_source(), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "body_keying_demo")
    output_base = tmp_path / "out"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    before = {
        probe.stage: (
            probe.source_observation.rerun.fingerprint,
            probe.source_observation.review.fingerprint,
            probe.decision_key,
        )
        for probe in probe_pipeline(
            pipeline, pipeline.Config(), args=pipeline.Args(), out=output_base / "main"
        )
    }

    text = module_path.read_text(encoding="utf-8")
    module_path.write_text(
        text.replace(
            '(ctx.out / "one.txt").write_text(str(HELPER), encoding="utf-8")',
            '(ctx.out / "one.txt").write_text(str(HELPER) + "!", encoding="utf-8")',
        ),
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "body_keying_demo")
    after = {
        probe.stage: probe
        for probe in probe_pipeline(
            pipeline, pipeline.Config(), args=pipeline.Args(), out=output_base / "main"
        )
    }

    assert after["one"].source_observation.rerun.fingerprint != before["one"][0]
    assert after["one"].source_observation.review.fingerprint == before["one"][1]
    assert after["one"].decision_key != before["one"][2]
    assert after["one"].decision.status == "needs-run"
    assert after["one"].decision.reason == "source-changed"
    assert after["one"].source_review.relationship == "current"
    assert after["two"].source_observation.rerun.fingerprint == before["two"][0]
    assert after["two"].source_observation.review.fingerprint == before["two"][1]
    assert after["two"].decision_key == before["two"][2]
    assert after["two"].decision.status == "hit"


@pytest.mark.parametrize(
    ("case", "before", "after"),
    [
        (
            "decorator",
            '@stage(produces="artifact.txt")',
            '@stage(produces=["artifact.txt"])',
        ),
        ("signature", "def build(self, ctx):", "def build(self, ctx) -> None:"),
    ],
)
def test_stage_decorator_and_signature_enter_rerun_fingerprint(
    tmp_path: Path,
    case: str,
    before: str,
    after: str,
) -> None:
    module_path = tmp_path / f"{case}.py"
    module_path.write_text(_source('(ctx.out / "artifact.txt").write_text("same")'))
    pipeline = _load_pipeline(module_path, f"{case}_keying_demo")
    first = SourceFingerprintSession().observe(pipeline, pipeline.stages()["build"])
    module_path.write_text(module_path.read_text().replace(before, after))
    pipeline = _load_pipeline(module_path, f"{case}_keying_demo")
    second = SourceFingerprintSession().observe(pipeline, pipeline.stages()["build"])
    assert second.rerun.fingerprint != first.rerun.fingerprint
    assert second.review.fingerprint == first.review.fingerprint


def test_shared_helper_change_opens_review_for_both_stages(tmp_path: Path) -> None:
    module_path = tmp_path / "shared_helper.py"
    module_path.write_text(_two_stage_shared_helper_source(), encoding="utf-8")
    pipeline = _load_pipeline(module_path, "helper_keying_demo")
    output_base = tmp_path / "out"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    module_path.write_text(
        module_path.read_text(encoding="utf-8").replace("HELPER = 1", "HELPER = 2"),
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "helper_keying_demo")
    probes = {
        probe.stage: probe
        for probe in probe_pipeline(
            pipeline, pipeline.Config(), args=pipeline.Args(), out=output_base / "main"
        )
    }
    assert probes["one"].source_review == SourceReviewState("changed")
    assert probes["two"].source_review == SourceReviewState("changed")
    assert probes["one"].decision.status == "hit"
    assert probes["two"].decision.status == "hit"


def test_declared_sources_enter_rerun_and_review_sources_enter_review(tmp_path: Path) -> None:
    rerun_helper = tmp_path / "rerun_helper.py"
    review_helper = tmp_path / "review_helper.py"
    rerun_helper.write_text("VALUE = 1\n", encoding="utf-8")
    review_helper.write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "declared.py"
    module_path.write_text(
        """from pathlib import Path
from pydantic import BaseModel
from varve import Dependencies, Pipeline, stage

class Config(BaseModel):
    pass

class Demo(Pipeline):
    Config = Config

    @stage(
        produces="artifact.txt",
        depends=Dependencies(
            sources=[Path("rerun_helper.py")],
            review_sources=[Path("review_helper.py")],
        ),
    )
    def build(self, ctx):
        (ctx.out / "artifact.txt").write_text("same", encoding="utf-8")
""",
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "declared_keying_demo")
    output_base = tmp_path / "out"
    run(pipeline, pipeline.Config(), cli_out=output_base)

    rerun_helper.write_text("VALUE = 2\n", encoding="utf-8")
    rerun_probe = probe_pipeline(
        pipeline, pipeline.Config(), args=pipeline.Args(), out=output_base / "main"
    )[0]
    assert rerun_probe.decision.status == "needs-run"
    assert rerun_probe.decision.reason == "source-changed"
    assert rerun_probe.source_review.relationship == "current"

    rerun_helper.write_text("VALUE = 1\n", encoding="utf-8")
    review_helper.write_text("VALUE = 2\n", encoding="utf-8")
    review_probe = probe_pipeline(
        pipeline, pipeline.Config(), args=pipeline.Args(), out=output_base / "main"
    )[0]
    assert review_probe.source_review == SourceReviewState("changed")
    assert review_probe.decision.status == "hit"


def test_same_declared_root_in_sources_and_review_sources_fast_fails(tmp_path: Path) -> None:
    helper = tmp_path / "helper.py"
    helper.write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "conflict.py"
    module_path.write_text(
        """from pathlib import Path
from pydantic import BaseModel
from varve import Dependencies, Pipeline, stage

class Config(BaseModel):
    pass

class Demo(Pipeline):
    Config = Config

    @stage(
        produces="artifact.txt",
        depends=Dependencies(
            sources=[Path("helper.py")],
            review_sources=[Path("helper.py")],
        ),
    )
    def build(self, ctx):
        (ctx.out / "artifact.txt").write_text("same", encoding="utf-8")
""",
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "conflict_keying_demo")
    probe = probe_pipeline(
        pipeline, pipeline.Config(), args=pipeline.Args(), out=tmp_path / "out" / "main"
    )[0]
    assert probe.decision.status == "error"
    assert "same root" in probe.decision.reason


def test_overlapping_declared_files_prefer_rerun(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "member.py").write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "overlap.py"
    module_path.write_text(
        """from pathlib import Path
from pydantic import BaseModel
from varve import Dependencies, Pipeline, stage

class Config(BaseModel):
    pass

class Demo(Pipeline):
    Config = Config

    @stage(
        produces="artifact.txt",
        depends=Dependencies(
            sources=[Path("shared")],
            review_sources=[Path("shared/member.py")],
        ),
    )
    def build(self, ctx):
        (ctx.out / "artifact.txt").write_text("same", encoding="utf-8")
""",
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "overlap_keying_demo")
    output_base = tmp_path / "out"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    (shared / "member.py").write_text("VALUE = 2\n", encoding="utf-8")
    probe = probe_pipeline(
        pipeline, pipeline.Config(), args=pipeline.Args(), out=output_base / "main"
    )[0]
    assert probe.decision.status == "needs-run"
    assert probe.decision.reason == "source-changed"
    assert probe.source_review.relationship == "current"
    assert any(entry.path.startswith("declared:") for entry in probe.source_observation.rerun.files)
    assert not any(
        entry.path.startswith("review:") for entry in probe.source_observation.review.files
    )


def test_local_class_factory_callable_is_located(tmp_path: Path) -> None:
    module_path = tmp_path / "factory.py"
    module_path.write_text(
        """from pathlib import Path
from pydantic import BaseModel
from varve import Pipeline, stage

class Config(BaseModel):
    pass

def build_effect_experiment():
    class Demo(Pipeline):
        Config = Config

        @stage(produces="artifact.txt")
        def build(self, ctx):
            (ctx.out / "artifact.txt").write_text("ok", encoding="utf-8")

    return Demo

Demo = build_effect_experiment()
""",
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "factory_keying_demo")
    probe = probe_pipeline(
        pipeline, pipeline.Config(), args=pipeline.Args(), out=tmp_path / "out" / "main"
    )[0]
    assert probe.decision.status == "needs-run"
    assert probe.source_observation.rerun.fingerprint != "error"
    assert any(entry.path == "callable:build" for entry in probe.source_observation.rerun.files)


def test_checkout_absolute_path_does_not_enter_fingerprint(tmp_path: Path) -> None:
    module_path = tmp_path / "abs_path.py"
    module_path.write_text(_source('(ctx.out / "artifact.txt").write_text("same")'))
    pipeline = _load_pipeline(module_path, "abs_path_keying_demo")
    observation = probe_pipeline(
        pipeline, pipeline.Config(), args=pipeline.Args(), out=tmp_path / "out" / "main"
    )[0].source_observation
    for entry in (*observation.rerun.files, *observation.review.files):
        assert str(tmp_path) not in entry.path
        assert str(tmp_path) not in entry.digest


def test_declared_source_directory_manifest_changes_fingerprint(tmp_path: Path) -> None:
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    (helpers / "first.py").write_text("VALUE = 1\n", encoding="utf-8")
    module_path = tmp_path / "declared_dir.py"
    module_path.write_text(
        """from pathlib import Path
from pydantic import BaseModel
from varve import Dependencies, Pipeline, stage

class Config(BaseModel):
    pass

class Demo(Pipeline):
    Config = Config

    @stage(produces="artifact.txt", depends=Dependencies(sources=[Path("helpers")]))
    def build(self, ctx):
        (ctx.out / "artifact.txt").write_text("same", encoding="utf-8")
""",
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "declared_dir_keying_demo")
    output_base = tmp_path / "out"
    run(pipeline, pipeline.Config(), cli_out=output_base)
    first = probe_pipeline(
        pipeline, pipeline.Config(), args=pipeline.Args(), out=output_base / "main"
    )[0].source_observation.rerun.fingerprint

    (helpers / "second.py").write_text("VALUE = 2\n", encoding="utf-8")
    second = probe_pipeline(
        pipeline, pipeline.Config(), args=pipeline.Args(), out=output_base / "main"
    )[0]
    assert second.source_observation.rerun.fingerprint != first
    assert second.decision.reason == "source-changed"


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


def test_residual_ast_retries_when_file_changes_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_path = tmp_path / "racing_source.py"
    initial = _two_stage_shared_helper_source()
    changed = initial.replace("HELPER = 1", "HELPER = 2")
    module_path.write_text(initial, encoding="utf-8")
    pipeline = _load_pipeline(module_path, "racing_source_demo")
    stage_spec = pipeline.stages()["one"]
    baseline = SourceFingerprintSession().observe(pipeline, stage_spec)
    original_read = source_module._read_module_ast
    reads = 0

    def racing_read(path: Path):
        nonlocal reads
        tree, after = original_read(path)
        if path == module_path and reads == 0:
            module_path.write_text(changed, encoding="utf-8")
            after = module_path.stat()
        reads += 1
        return tree, after

    monkeypatch.setattr(source_module, "_read_module_ast", racing_read)
    observed = SourceFingerprintSession().observe(pipeline, stage_spec)

    assert reads == 2
    assert observed.review.fingerprint != baseline.review.fingerprint


def test_unlocatable_stage_callable_is_an_evaluation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module_path = tmp_path / "unlocatable.py"
    module_path.write_text(_source('(ctx.out / "artifact.txt").write_text("same")'))
    pipeline = _load_pipeline(module_path, "unlocatable_source_demo")
    stage_func = pipeline.stages()["build"].func
    original_getsourcefile = source_module.inspect.getsourcefile
    monkeypatch.setattr(
        source_module.inspect,
        "getsourcefile",
        lambda value: None if value is stage_func else original_getsourcefile(value),
    )

    probe = probe_pipeline(
        pipeline,
        pipeline.Config(),
        args=pipeline.Args(),
        out=tmp_path / "out" / "main",
    )[0]

    assert probe.decision.status == "error"
    assert "Cannot locate Python source file for stage build" in probe.decision.reason


def test_ambiguous_stage_callable_ast_is_an_evaluation_error(tmp_path: Path) -> None:
    module_path = tmp_path / "ambiguous.py"
    module_path.write_text(
        """from pydantic import BaseModel
from varve import Pipeline, stage

class Config(BaseModel):
    pass

def make_build():
    def build():
        def build(self, ctx):
            (ctx.out / "artifact.txt").write_text("same", encoding="utf-8")
        return build
    return build()

class Demo(Pipeline):
    Config = Config
    build = stage(produces="artifact.txt")(make_build())
""",
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, "ambiguous_source_demo")

    probe = probe_pipeline(
        pipeline,
        pipeline.Config(),
        args=pipeline.Args(),
        out=tmp_path / "out" / "main",
    )[0]

    assert probe.decision.status == "error"
    assert "Cannot uniquely locate Stage callable AST" in probe.decision.reason
    assert "found 2 candidates" in probe.decision.reason


def test_source_parser_honors_pep_263_encoding(tmp_path: Path) -> None:
    module_path = tmp_path / "latin1_demo.py"
    source = "# coding: latin-1\n" + _source(
        '(ctx.out / "artifact.txt").write_text("é", encoding="utf-8")'
    )
    module_path.write_bytes(source.encode("latin-1"))
    pipeline = _load_pipeline(module_path, "latin1_keying_demo")
    probe = probe_pipeline(
        pipeline,
        pipeline.Config(),
        args=pipeline.Args(),
        out=tmp_path / "out" / "main",
    )[0]
    assert probe.decision.status == "needs-run"
    assert probe.source_observation.rerun.fingerprint != "error"


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
    module_path.write_text(
        f"""from pathlib import Path
from pydantic import BaseModel
from varve import Dependencies, Pipeline, stage

class Config(BaseModel):
    pass

class Demo(Pipeline):
    Config = Config

    @stage(
        produces="artifact.txt",
        depends=Dependencies(sources=[Path("{declared_name}")]),
    )
    def build(self, ctx):
        (ctx.out / "artifact.txt").write_text("same", encoding="utf-8")
""",
        encoding="utf-8",
    )
    pipeline = _load_pipeline(module_path, f"invalid_source_{declared_name.replace('.', '_')}")
    probe = probe_pipeline(
        pipeline,
        pipeline.Config(),
        args=pipeline.Args(),
        out=tmp_path / "out" / "main",
    )[0]
    assert probe.decision.status == "error"
    assert message in probe.decision.reason
