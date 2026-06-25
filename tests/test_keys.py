from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import BaseModel

from varve.decorators import StageSpec, stage
from varve.keying.keys import compute_key_components, content_key, run_key
from varve.keyspec import KeySpec
from varve.models import (
    KeyComponents,
    OutputHandle,
    ProducedPath,
    SuccessRecord,
)


class Config(BaseModel):
    profile: str
    limit: int = 10


class Ctx:
    def __init__(self, config: Config) -> None:
        self.config = config


def helper(value: str) -> str:
    return value.upper()


@stage(uses=[helper])
def transform(self, ctx):  # pragma: no cover - inspected only
    return helper(ctx.config.profile)


def missing_helper(value: str) -> str:
    return value.lower()


@stage()
def transform_missing_uses(self, ctx):  # pragma: no cover - inspected only
    return missing_helper(ctx.config.profile)


def _stage_spec() -> StageSpec:
    return transform.__varve_stage__


def _load_module(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_content_key_changes_when_config_changes(tmp_path: Path) -> None:
    first = compute_key_components(_stage_spec(), Ctx(Config(profile="a")), {})
    second = compute_key_components(_stage_spec(), Ctx(Config(profile="b")), {})
    assert content_key(first) != content_key(second)


def test_content_key_uses_all_config_fields() -> None:
    first = compute_key_components(_stage_spec(), Ctx(Config(profile="a", limit=1)), {})
    second = compute_key_components(_stage_spec(), Ctx(Config(profile="a", limit=2)), {})
    assert content_key(first) != content_key(second)


def test_content_key_changes_when_value_changes(tmp_path: Path) -> None:
    def value_one(_ctx):
        return {"logic": 1}

    def value_two(_ctx):
        return {"logic": 2}

    base = _stage_spec()
    one = StageSpec(
        name=base.name,
        kind=base.kind,
        func=base.func,
        needs=base.needs,
        produces=base.produces,
        keyspec=KeySpec(values={"logic": value_one}),
        uses=base.uses,
    )
    two = StageSpec(
        name=base.name,
        kind=base.kind,
        func=base.func,
        needs=base.needs,
        produces=base.produces,
        keyspec=KeySpec(values={"logic": value_two}),
        uses=base.uses,
    )
    ctx = Ctx(Config(profile="a"))
    assert content_key(compute_key_components(one, ctx, {})) != content_key(
        compute_key_components(two, ctx, {})
    )


def test_content_key_files_use_sha_not_mtime(tmp_path: Path) -> None:
    data = tmp_path / "data.txt"
    data.write_text("same bytes", encoding="utf-8")

    spec = StageSpec(
        name="sample",
        kind="single",
        func=transform,
        needs=(),
        produces=None,
        keyspec=KeySpec(files={"data": lambda _ctx: data}),
        uses=(helper,),
    )
    ctx = Ctx(Config(profile="a"))
    first = compute_key_components(spec, ctx, {})
    key = content_key(first)

    data.touch()
    second = compute_key_components(spec, ctx, {}, cached_files=first.files)
    assert content_key(second) == key
    assert second.files["data"][0].mtime != first.files["data"][0].mtime


def test_varve_decorator_arguments_do_not_enter_source_hash(tmp_path: Path) -> None:
    first_module_path = tmp_path / "first_module.py"
    first_module_path.write_text(
        """
from varve.decorators import batch_stage

@batch_stage(partition_key=["small"])
async def partitioned(self, ctx):
    yield ctx
""",
        encoding="utf-8",
    )
    second_module_path = tmp_path / "second_module.py"
    second_module_path.write_text(
        """
from varve.decorators import batch_stage

@batch_stage(partition_key=["large"])
async def partitioned(self, ctx):
    yield ctx
""",
        encoding="utf-8",
    )

    first_module = _load_module(first_module_path, "first_module")
    second_module = _load_module(second_module_path, "second_module")
    first = compute_key_components(
        first_module.partitioned.__varve_stage__,
        Ctx(Config(profile="a")),
        {},
    )
    second = compute_key_components(
        second_module.partitioned.__varve_stage__,
        Ctx(Config(profile="a")),
        {},
    )

    assert content_key(first) == content_key(second)
    assert run_key(content_key(first), {"partition": "small"}) != run_key(
        content_key(first),
        {"partition": "large"},
    )


def test_uses_helpers_with_same_qualname_do_not_overwrite_each_other(tmp_path: Path) -> None:
    first_module_path = tmp_path / "uses_first.py"
    first_module_path.write_text(
        """
def helper(value):
    return value + 1
""",
        encoding="utf-8",
    )
    second_module_path = tmp_path / "uses_second.py"
    second_module_path.write_text(
        """
def helper(value):
    return value + 2
""",
        encoding="utf-8",
    )
    first_module = _load_module(first_module_path, "uses_first")
    second_module = _load_module(second_module_path, "uses_second")

    base = _stage_spec()
    spec = StageSpec(
        name=base.name,
        kind=base.kind,
        func=base.func,
        needs=base.needs,
        produces=base.produces,
        keyspec=base.keyspec,
        uses=(helper, first_module.helper, second_module.helper),
    )
    components = compute_key_components(spec, Ctx(Config(profile="a")), {})

    assert "uses.uses_first.helper" in components.source
    assert "uses.uses_second.helper" in components.source
    assert len([key for key in components.source if key.startswith("uses.")]) == 3


def test_content_key_changes_when_upstream_changes(tmp_path: Path) -> None:
    base = _stage_spec()
    spec = StageSpec(
        name=base.name,
        kind=base.kind,
        func=base.func,
        needs=("sample",),
        produces=base.produces,
        keyspec=base.keyspec,
        uses=base.uses,
    )
    ctx = Ctx(Config(profile="a"))
    one = compute_key_components(spec, ctx, {"sample": "sha256:one"})
    two = compute_key_components(spec, ctx, {"sample": "sha256:two"})
    assert content_key(one) != content_key(two)


class PathConfig(BaseModel):
    profile: str
    workspace: Path


class NestedPathConfig(BaseModel):
    workspace: Path | None = None


class OuterPathConfig(BaseModel):
    nested: NestedPathConfig = NestedPathConfig()


class MappingPathConfig(BaseModel):
    payload: dict[Any, str]


class PathCtx:
    def __init__(self, config: PathConfig | OuterPathConfig | MappingPathConfig) -> None:
        self.config = config


def test_config_path_fields_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="Config fields must not contain Path"):
        compute_key_components(_stage_spec(), PathCtx(PathConfig(profile="a", workspace=tmp_path)), {})


def test_nested_config_path_fields_are_rejected_even_when_none() -> None:
    with pytest.raises(TypeError, match="Config fields must not contain Path"):
        compute_key_components(_stage_spec(), PathCtx(OuterPathConfig()), {})


def test_config_path_values_inside_mapping_keys_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(TypeError, match="Config fields must not contain Path"):
        compute_key_components(
            _stage_spec(),
            PathCtx(MappingPathConfig(payload={tmp_path: "data"})),
            {},
        )


def test_unregistered_same_module_helper_is_rejected() -> None:
    with pytest.raises(ValueError, match="missing_helper"):
        compute_key_components(
            transform_missing_uses.__varve_stage__,
            Ctx(Config(profile="a")),
            {},
        )


def test_run_key_includes_content_key_and_partition() -> None:
    assert run_key("sha256:a", {"batch": 1}) == run_key("sha256:a", {"batch": 1})
    assert run_key("sha256:a", {"batch": 1}) != run_key("sha256:b", {"batch": 1})
    assert run_key("sha256:a", {"batch": 1}) != run_key("sha256:a", {"batch": 2})


def test_success_record_enforces_kind_specific_output_shape() -> None:
    components = KeyComponents(source={}, config={}, files={}, values={}, upstreams={})
    SuccessRecord(
        experiment="Demo",
        stage="sample",
        kind="single",
        content_key="sha256:x",
        key_components=components,
        produces=[ProducedPath(path="sample.txt", kind="file")],
        committed_at="now",
    )
    SuccessRecord(
        experiment="Demo",
        stage="transform",
        kind="batch",
        content_key="sha256:x",
        key_components=components,
        outputs=[OutputHandle(index=0, path="part-0.txt")],
        committed_at="now",
    )

    with pytest.raises(ValueError, match="single success records"):
        SuccessRecord(
            experiment="Demo",
            stage="sample",
            kind="single",
            content_key="sha256:x",
            key_components=components,
            outputs=[OutputHandle(index=0, path="part-0.txt")],
            committed_at="now",
        )
    with pytest.raises(ValueError, match="batch success records"):
        SuccessRecord(
            experiment="Demo",
            stage="transform",
            kind="batch",
            content_key="sha256:x",
            key_components=components,
            produces=[ProducedPath(path="sample.txt", kind="file")],
            committed_at="now",
        )
