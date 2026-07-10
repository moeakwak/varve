from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from pydantic import BaseModel

from varve.decorators import StageSpec, stage
from varve.keying.keys import compute_key_components, content_key
from varve.keyspec import KeySpec
from varve.models import (
    KeyComponents,
    OutputHandle,
    ProducedPath,
    SuccessRecord,
)
from varve.pipeline import Pipeline


class Config(BaseModel):
    profile: str
    limit: int = 10


class Ctx:
    def __init__(self, config: Config) -> None:
        self.config = config


def helper(value: str) -> str:
    return value.upper()


AUTO_VALUE = {"mode": "strict"}


@stage()
def transform(self, ctx):  # pragma: no cover - inspected only
    return helper(ctx.config.profile)


def missing_helper(value: str) -> str:
    return value.lower()


@stage(auto_uses=False)
def transform_without_auto_uses(self, ctx):  # pragma: no cover - inspected only
    return missing_helper(ctx.config.profile)


@stage()
def transform_with_auto_value(self, ctx):  # pragma: no cover - inspected only
    return ctx.config.profile, AUTO_VALUE


def _stage_spec() -> StageSpec:
    return transform.__varve_stage__


def _load_module(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stage_captures_explicit_uses() -> None:
    @stage(uses=[helper])
    def sample(self, ctx):  # pragma: no cover - inspected only
        return helper(ctx.config.profile)

    assert sample.__varve_stage__.uses == (helper,)


def test_stage_rejects_removed_explicit_uses_name() -> None:
    removed_name = "additional" + "_uses"
    with pytest.raises(TypeError, match=removed_name):
        stage(**{removed_name: [helper]})  # type: ignore[arg-type]


def test_pipeline_auto_uses_packages_defaults_to_none() -> None:
    assert Pipeline.auto_uses_packages is None


def test_pipeline_can_disable_package_recursion() -> None:
    class Demo(Pipeline):
        Config = Config
        auto_uses_packages = ()

        @stage()
        def sample(self, ctx):  # pragma: no cover - inspected only
            return ctx.config.profile

    assert Demo.auto_uses_packages == ()


def test_content_key_changes_when_config_changes(tmp_path: Path) -> None:
    first = compute_key_components(_stage_spec(), Ctx(Config(profile="a")), {})
    second = compute_key_components(_stage_spec(), Ctx(Config(profile="b")), {})
    assert content_key(first) != content_key(second)


def test_content_key_uses_all_config_fields() -> None:
    first = compute_key_components(_stage_spec(), Ctx(Config(profile="a", limit=1)), {})
    second = compute_key_components(_stage_spec(), Ctx(Config(profile="a", limit=2)), {})
    assert content_key(first) != content_key(second)


def test_content_key_projects_onto_config_access() -> None:
    spec = _stage_spec()
    base = compute_key_components(
        spec, Ctx(Config(profile="a", limit=1)), {}, config_access=["profile"]
    )
    unread = compute_key_components(
        spec, Ctx(Config(profile="a", limit=2)), {}, config_access=["profile"]
    )
    read = compute_key_components(
        spec, Ctx(Config(profile="b", limit=1)), {}, config_access=["profile"]
    )
    assert base.config == {"profile": "a"}
    assert base.config_access == ["profile"]
    assert content_key(base) == content_key(unread)  # limit is outside the access set
    assert content_key(base) != content_key(read)  # profile is inside it


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
        auto_uses=base.auto_uses,
        uses=base.uses,
    )
    two = StageSpec(
        name=base.name,
        kind=base.kind,
        func=base.func,
        needs=base.needs,
        produces=base.produces,
        keyspec=KeySpec(values={"logic": value_two}),
        auto_uses=base.auto_uses,
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
        auto_uses=False,
        uses=(helper,),
    )
    ctx = Ctx(Config(profile="a"))
    first = compute_key_components(spec, ctx, {})
    key = content_key(first)

    data.touch()
    second = compute_key_components(spec, ctx, {}, cached_files=first.files)
    assert content_key(second) == key
    assert second.files["data"][0].mtime != first.files["data"][0].mtime


def test_uses_helpers_with_same_qualname_do_not_overwrite_each_other(
    tmp_path: Path,
) -> None:
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
        auto_uses=False,
        uses=(helper, first_module.helper, second_module.helper),
    )
    components = compute_key_components(spec, Ctx(Config(profile="a")), {})

    assert "uses.function.uses_first.helper" in components.source
    assert "uses.function.uses_second.helper" in components.source
    assert len([key for key in components.source if key.startswith("uses.function.")]) == 3


def test_main_module_uses_stable_spec_name_for_helper_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Spec:
        name = "pkg.demo.__main__"

    module = type("Module", (), {"__spec__": Spec()})()
    monkeypatch.setitem(sys.modules, "__main__", module)
    original_module = helper.__module__
    helper.__module__ = "__main__"
    try:
        base = _stage_spec()
        spec = StageSpec(
            name=base.name,
            kind=base.kind,
            func=base.func,
            needs=base.needs,
            produces=base.produces,
            keyspec=base.keyspec,
            auto_uses=False,
            uses=(helper,),
        )

        components = compute_key_components(spec, Ctx(Config(profile="a")), {})
    finally:
        helper.__module__ = original_module

    assert "uses.function.pkg.demo.__main__.helper" in components.source
    assert "uses.function.__main__.helper" not in components.source


def test_main_module_helpers_are_auto_detected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Spec:
        name = "pkg.demo.__main__"

    module = type("Module", (), {"__spec__": Spec()})()
    monkeypatch.setitem(sys.modules, "__main__", module)
    original_transform_module = transform.__module__
    original_helper_module = helper.__module__
    transform.__module__ = "__main__"
    helper.__module__ = "__main__"
    try:
        components = compute_key_components(_stage_spec(), Ctx(Config(profile="a")), {})
    finally:
        transform.__module__ = original_transform_module
        helper.__module__ = original_helper_module

    assert "auto.function.pkg.demo.__main__.helper" in components.source
    assert "auto.function.__main__.helper" not in components.source


def test_content_key_changes_when_upstream_changes(tmp_path: Path) -> None:
    base = _stage_spec()
    spec = StageSpec(
        name=base.name,
        kind=base.kind,
        func=base.func,
        needs=("sample",),
        produces=base.produces,
        keyspec=base.keyspec,
        auto_uses=base.auto_uses,
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
        compute_key_components(
            _stage_spec(), PathCtx(PathConfig(profile="a", workspace=tmp_path)), {}
        )


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


def test_auto_uses_registers_same_module_helper() -> None:
    components = compute_key_components(_stage_spec(), Ctx(Config(profile="a")), {})

    assert "auto.function.test_keys.helper" in components.source


def test_auto_uses_registers_imported_project_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    package = tmp_path / "demo_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "helpers.py").write_text(
        """
def nested(value):
    return value + "!"

def exported(value):
    return nested(value).upper()
""",
        encoding="utf-8",
    )
    (package / "stages.py").write_text(
        """
from varve.decorators import stage
from demo_pkg.helpers import exported

@stage()
def sample(self, ctx):
    return exported(ctx.config.profile)
""",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    module = importlib.import_module("demo_pkg.stages")

    components = compute_key_components(
        module.sample.__varve_stage__,
        Ctx(Config(profile="a")),
        {},
    )

    assert "auto.function.demo_pkg.helpers.exported" in components.source
    assert "auto.function.demo_pkg.helpers.nested" in components.source


def test_auto_uses_disabled_does_not_reject_unlisted_helper() -> None:
    components = compute_key_components(
        transform_without_auto_uses.__varve_stage__,
        Ctx(Config(profile="a")),
        {},
    )
    assert set(components.source) == {"stage"}


def test_explicit_uses_use_distinct_namespace() -> None:
    base = _stage_spec()
    spec = StageSpec(
        name=base.name,
        kind=base.kind,
        func=base.func,
        needs=base.needs,
        produces=base.produces,
        keyspec=base.keyspec,
        auto_uses=False,
        uses=(helper,),
    )
    components = compute_key_components(spec, Ctx(Config(profile="a")), {})
    assert "uses.function.test_keys.helper" in components.source


def test_auto_value_changes_source_key_not_explicit_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = transform_with_auto_value.__varve_stage__
    first = compute_key_components(spec, Ctx(Config(profile="a")), {})
    monkeypatch.setitem(transform_with_auto_value.__globals__, "AUTO_VALUE", {"mode": "loose"})
    second = compute_key_components(spec, Ctx(Config(profile="a")), {})

    locator = "auto.value.test_keys.transform_with_auto_value.global.AUTO_VALUE"
    assert locator in first.source
    assert first.source[locator] != second.source[locator]
    assert first.values == second.values == {}
    assert content_key(first) != content_key(second)


def test_success_record_enforces_kind_specific_output_shape() -> None:
    components = KeyComponents(source={}, config={}, files={}, values={}, upstreams={})
    SuccessRecord(
        pipeline="Demo",
        stage="sample",
        kind="single",
        content_key="sha256:x",
        key_components=components,
        produces=[ProducedPath(path="sample.txt", kind="file")],
        committed_at="now",
    )
    SuccessRecord(
        pipeline="Demo",
        stage="transform",
        kind="batch",
        content_key="sha256:x",
        key_components=components,
        outputs=[OutputHandle(index=0, path="part-0.txt")],
        committed_at="now",
    )

    with pytest.raises(ValueError, match="single success records"):
        SuccessRecord(
            pipeline="Demo",
            stage="sample",
            kind="single",
            content_key="sha256:x",
            key_components=components,
            outputs=[OutputHandle(index=0, path="part-0.txt")],
            committed_at="now",
        )
    with pytest.raises(ValueError, match="batch success records"):
        SuccessRecord(
            pipeline="Demo",
            stage="transform",
            kind="batch",
            content_key="sha256:x",
            key_components=components,
            produces=[ProducedPath(path="sample.txt", kind="file")],
            committed_at="now",
        )
