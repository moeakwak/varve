from __future__ import annotations

import pickle

from pydantic import BaseModel, Field

from varve.keying.config_access import ConfigAccess, RecordingConfig, project_config


class Nested(BaseModel):
    a: int = 1
    b: int = 2


class Config(BaseModel):
    datasets: list[str] = Field(default_factory=lambda: ["mw", "im2latex"])
    tools: list[str] = Field(default_factory=lambda: ["identity", "texform"])
    seed: int = 42
    nested: Nested = Field(default_factory=Nested)


def _wrap(config: Config) -> tuple[RecordingConfig, ConfigAccess]:
    access = ConfigAccess()
    return RecordingConfig(config, access), access


def test_plain_field_read_is_recorded_precisely() -> None:
    proxy, access = _wrap(Config())
    _ = proxy.tools
    _ = proxy.seed
    assert access.resolve() == ["seed", "tools"]


def test_helper_through_wrapper_records_reads() -> None:
    def helper(config) -> int:
        return len(config.datasets)

    proxy, access = _wrap(Config())
    helper(proxy)
    assert access.resolve() == ["datasets"]


def test_dynamic_getattr_of_field_is_precise() -> None:
    proxy, access = _wrap(Config())
    _ = getattr(proxy, "tools")
    assert access.resolve() == ["tools"]


def test_dynamic_getattr_of_unknown_name_marks_all() -> None:
    proxy, access = _wrap(Config())
    _ = getattr(proxy, "nope", None)
    assert access.resolve() is None


def test_model_dump_marks_all() -> None:
    proxy, access = _wrap(Config())
    proxy.model_dump()
    assert access.resolve() is None


def test_iteration_marks_all() -> None:
    config = Config()
    proxy, access = _wrap(config)
    assert dict(proxy) == dict(config)
    assert access.resolve() is None


def test_dunder_dict_marks_all() -> None:
    proxy, access = _wrap(Config())
    _ = proxy.__dict__
    assert access.resolve() is None


def test_pickle_marks_all_and_yields_real_config() -> None:
    config = Config()
    proxy, access = _wrap(config)
    restored = pickle.loads(pickle.dumps(proxy))
    assert isinstance(restored, Config)
    assert restored.tools == config.tools
    assert access.resolve() is None


def test_nested_read_records_top_level_field() -> None:
    proxy, access = _wrap(Config())
    _ = proxy.nested.a + proxy.nested.b
    assert access.resolve() == ["nested"]


def test_wrapper_returns_identical_values() -> None:
    config = Config()
    proxy, _ = _wrap(config)
    assert proxy.tools == config.tools
    assert proxy.nested.a == config.nested.a


def test_project_config_none_returns_everything() -> None:
    dump = Config().model_dump()
    assert project_config(dump, None) == dump


def test_project_config_subset() -> None:
    dump = Config().model_dump()
    assert project_config(dump, ["seed", "tools"]) == {
        "seed": dump["seed"],
        "tools": dump["tools"],
    }


def test_project_config_skips_missing_fields() -> None:
    dump = Config().model_dump()
    assert project_config(dump, ["tools", "removed_field"]) == {"tools": dump["tools"]}
