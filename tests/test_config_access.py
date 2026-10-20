from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from functools import singledispatch
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

from varve import Pipeline, stage
from varve.engine.runner import run
from varve.keying.config_access import (
    ConfigAccess,
    RecordingConfig,
    RecordingDict,
    project_config,
    record_config,
    unwrap_config,
)
from varve.keying.keys import config_data


class Nested(BaseModel):
    a: int = 1
    b: int = 2


class Config(BaseModel):
    datasets: list[str] = Field(default_factory=lambda: ["mw", "im2latex"])
    tools: list[str] = Field(default_factory=lambda: ["identity", "texform"])
    seed: int = 42
    nested: Nested = Field(default_factory=Nested)


def _wrap(config) -> tuple:
    access = ConfigAccess()
    return record_config(config, access), access


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


def test_pydantic_proxy_passes_isinstance_and_singledispatch() -> None:
    proxy, access = _wrap(Config())

    @singledispatch
    def kind(value) -> str:
        return "other"

    @kind.register
    def _(value: Config) -> str:
        return "config"

    assert isinstance(proxy, RecordingConfig)
    assert isinstance(proxy, Config)
    assert isinstance(proxy, BaseModel)
    assert kind(proxy) == "config"
    assert access.resolve() == []


@pytest.mark.parametrize("other", [Config(), Config(seed=1)])
def test_pydantic_equality_matches_original_and_marks_all(other: Config) -> None:
    config = Config()
    for compare in (lambda p: p == other, lambda p: other == p, lambda p: p == _wrap(other)[0]):
        proxy, access = _wrap(config)
        assert compare(proxy) == (config == other)
        assert access.resolve() is None


def test_pydantic_proxy_is_accepted_as_config_field() -> None:
    class Holder(BaseModel):
        config: Config

    proxy, access = _wrap(Config())
    holder = Holder(config=proxy)
    assert holder.config.seed == 42
    assert access.resolve() == ["seed"]
    holder.model_dump()
    assert access.resolve() is None


def test_pydantic_proxy_revalidated_field_reads_whole_config() -> None:
    class Strict(BaseModel):
        model_config = ConfigDict(revalidate_instances="always")
        seed: int = 1

    class Holder(BaseModel):
        config: Strict

    proxy, access = _wrap(Strict())
    assert type(Holder(config=proxy).config) is Strict
    assert access.resolve() is None


def test_pydantic_proxy_repr_and_unwrap() -> None:
    config = Config()
    proxy, access = _wrap(config)
    assert repr(proxy) == repr(config)
    assert access.resolve() is None
    proxy, access = _wrap(config)
    assert unwrap_config(proxy) is config
    assert access.resolve() is None
    proxy, access = _wrap(config)
    assert type(proxy.model_copy()) is Config
    assert access.resolve() is None


def test_proxies_are_read_only() -> None:
    proxy, _ = _wrap(Config())
    with pytest.raises(TypeError, match="read-only"):
        proxy.seed = 1
    mapping, _ = _wrap({"a": 1})
    with pytest.raises(TypeError, match="read-only"):
        mapping["a"] = 2
    with pytest.raises(TypeError, match="read-only"):
        mapping.update(a=2)


def test_dict_proxy_is_dict_and_records_top_level_keys() -> None:
    proxy, access = _wrap({"a": {"b": 1}, "seed": 3, "unused": 0})
    assert isinstance(proxy, RecordingDict)
    assert isinstance(proxy, dict)
    assert proxy["a"]["b"] == 1
    assert proxy.get("seed") == 3
    assert "seed" in proxy
    assert access.resolve() == ["a", "seed"]


@pytest.mark.parametrize(
    "read",
    [
        lambda p: p["missing"] if "missing" in p else None,
        lambda p: p.get("missing"),
        lambda p: list(p),
        lambda p: list(p.keys()),
        lambda p: list(p.items()),
        lambda p: list(p.values()),
        lambda p: len(p),
        lambda p: bool(p),
        lambda p: p == {"a": 1},
        lambda p: {"a": 1} == p,
        lambda p: repr(p),
        lambda p: dict(p),
        lambda p: {**p},
        lambda p: json.dumps(p),
        lambda p: p.copy(),
        lambda p: p | {},
        lambda p: pickle.loads(pickle.dumps(p)),
    ],
)
def test_dict_whole_access_marks_all(read) -> None:
    proxy, access = _wrap({"a": 1})
    read(proxy)
    assert access.resolve() is None


def test_dict_proxy_values_match_original() -> None:
    config = {"a": {"b": [1, 2]}, "c": "x"}
    proxy, _ = _wrap(config)
    assert proxy == config
    assert json.loads(json.dumps(proxy)) == config
    restored = pickle.loads(pickle.dumps(proxy))
    assert type(restored) is dict
    assert restored == config


def test_unsupported_config_types_are_rejected() -> None:
    @dataclass
    class Plain:
        seed: int = 1

    with pytest.raises(TypeError, match="pydantic BaseModel or a dict"):
        config_data(Plain())
    with pytest.raises(TypeError, match="keys must be strings"):
        config_data({1: "x"})
    with pytest.raises(TypeError, match="pydantic BaseModel or a dict"):
        run(DictPipeline, Plain(), cli_out=Path("unused"))


class DictPipeline(Pipeline):
    @stage(produces="greeting.txt")
    def greet(self, ctx):
        assert isinstance(ctx.config, dict)
        (ctx.out / "greeting.txt").write_text(ctx.config["greeting"]["text"])


def test_dict_config_runs_and_projects_unread_keys(tmp_path: Path) -> None:
    config = {"greeting": {"text": "hi"}, "unused": 1}
    assert [o.status for o in run(DictPipeline, config, cli_out=tmp_path)] == ["needs-run"]
    assert (tmp_path / "main" / "greeting.txt").read_text() == "hi"

    unread = run(DictPipeline, {**config, "unused": 2}, cli_out=tmp_path)
    assert [o.status for o in unread] == ["hit"]

    changed = {"greeting": {"text": "hello"}, "unused": 2}
    assert [o.status for o in run(DictPipeline, changed, cli_out=tmp_path)] == ["needs-run"]
    assert (tmp_path / "main" / "greeting.txt").read_text() == "hello"
