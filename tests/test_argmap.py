from __future__ import annotations

import argparse
from collections.abc import Mapping

import pytest
from pydantic import BaseModel, Field

from varve.cli.app import _config_from_args
from varve.cli.argmap import collect_cli_args_namespace, register_args


class ArgmapInner(BaseModel):
    name: str = "default"
    age: int = 0
    enabled: bool = True


class ArgmapConfig(BaseModel):
    target: str
    token: str = Field(default="abc", description="Authentication token.")
    batch_size: int = 8
    enabled: bool = True
    items: list[int] = Field(default_factory=list)
    inner: ArgmapInner = Field(default_factory=ArgmapInner)
    ratio: float | None = None
    env_value: str = "default-env"
    dotenv_value: str = "default-dotenv"
    default_value: str = "default"


class ConflictingConfig(BaseModel):
    target: str = "default-target"
    force: bool = False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    register_args(parser, ArgmapConfig)
    return parser


def test_register_and_collect_args(tmp_path) -> None:
    parser = _parser()

    namespace = parser.parse_args(
        [
            "--target",
            str(tmp_path),
            "--batch-size",
            "16",
            "--enabled",
            "--inner.name=x",
            "--items",
            "[1,2]",
            "--ratio",
            "0.5",
        ]
    )

    assert "token" not in vars(namespace)
    assert "inner.age" not in vars(namespace)
    assert "inner.name" not in vars(namespace)
    assert collect_cli_args_namespace(namespace, ArgmapConfig) == {
        "target": str(tmp_path),
        "batch_size": "16",
        "enabled": True,
        "items": [1, 2],
        "inner": {"name": "x"},
        "ratio": "0.5",
    }


def test_bool_fields_support_positive_and_negative_flags() -> None:
    parser = _parser()

    enabled = collect_cli_args_namespace(parser.parse_args(["--enabled"]), ArgmapConfig)
    disabled = collect_cli_args_namespace(parser.parse_args(["--no-enabled"]), ArgmapConfig)

    assert enabled == {"enabled": True}
    assert disabled == {"enabled": False}


def test_collect_only_uses_explicit_config_flags_when_names_conflict() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("target", nargs="?")
    parser.add_argument("--force", action="store_true")
    register_args(parser, ConflictingConfig)

    command_only = parser.parse_args(["sample", "--force"])
    explicit_config = parser.parse_args(["sample", "--force", "--target", "config"])

    assert collect_cli_args_namespace(command_only, ConflictingConfig) == {}
    assert collect_cli_args_namespace(explicit_config, ConflictingConfig) == {
        "target": "config",
    }


def test_deep_merge_keeps_nested_fields_from_multiple_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INNER__AGE", "99")
    parser = _parser()
    namespace = parser.parse_args(["--target", "out", "--inner.name", "from-cli"])

    config = _config_from_args(
        ArgmapConfig,
        init_kwargs=collect_cli_args_namespace(namespace, ArgmapConfig),
    )

    assert config.target == "out"
    assert config.inner.name == "from-cli"
    assert config.inner.age == 99


def test_priority_init_gt_env_gt_dotenv_gt_default(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "TOKEN=from-dotenv\nENV_VALUE=from-dotenv\nDOTENV_VALUE=from-dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TOKEN", "from-env")
    monkeypatch.setenv("ENV_VALUE", "from-env")

    parser = _parser()
    namespace = parser.parse_args(["--target", "out", "--token", "from-cli"])
    config = _config_from_args(
        ArgmapConfig,
        init_kwargs=collect_cli_args_namespace(namespace, ArgmapConfig),
    )

    assert config.target == "out"
    assert config.token == "from-cli"
    assert config.env_value == "from-env"
    assert config.dotenv_value == "from-dotenv"
    assert config.default_value == "default"
    assert config.batch_size == 8


@pytest.mark.parametrize(
    "config_type",
    [
        pytest.param(
            type(
                "DictConfig",
                (BaseModel,),
                {
                    "__annotations__": {"extra": dict[str, str]},
                    "extra": Field(default_factory=dict),
                },
            ),
            id="dict",
        ),
        pytest.param(
            type(
                "BareDictConfig",
                (BaseModel,),
                {
                    "__annotations__": {"extra": dict},
                    "extra": Field(default_factory=dict),
                },
            ),
            id="bare-dict",
        ),
        pytest.param(
            type(
                "MappingConfig",
                (BaseModel,),
                {
                    "__annotations__": {"extra": Mapping[str, str]},
                    "extra": Field(default_factory=dict),
                },
            ),
            id="mapping",
        ),
        pytest.param(
            type(
                "BareMappingConfig",
                (BaseModel,),
                {
                    "__annotations__": {"extra": Mapping},
                    "extra": Field(default_factory=dict),
                },
            ),
            id="bare-mapping",
        ),
        pytest.param(
            type(
                "TupleConfig",
                (BaseModel,),
                {
                    "__annotations__": {"extra": tuple[int, ...]},
                    "extra": (),
                },
            ),
            id="tuple",
        ),
        pytest.param(
            type(
                "BareTupleConfig",
                (BaseModel,),
                {
                    "__annotations__": {"extra": tuple},
                    "extra": (),
                },
            ),
            id="bare-tuple",
        ),
        pytest.param(
            type(
                "BareSetConfig",
                (BaseModel,),
                {
                    "__annotations__": {"extra": set},
                    "extra": Field(default_factory=set),
                },
            ),
            id="bare-set",
        ),
        pytest.param(
            type(
                "UnionConfig",
                (BaseModel,),
                {
                    "__annotations__": {"extra": int | str},
                    "extra": 0,
                },
            ),
            id="union",
        ),
    ],
)
def test_fast_fail_for_unsupported_field_types(config_type: type[BaseModel]) -> None:
    with pytest.raises(TypeError, match="argmap does not support args field"):
        register_args(argparse.ArgumentParser(), config_type)


def test_help_uses_readable_metavar_and_field_descriptions() -> None:
    help_text = _parser().format_help()

    assert "__VARVE_CONFIG__" not in help_text
    assert "__VARVE_ARGS__" not in help_text
    assert "--target TARGET" in help_text
    assert "--batch-size BATCH_SIZE" in help_text
    assert "--inner.name INNER_NAME" in help_text
    assert "Authentication token." in help_text
    assert "Set Args.batch_size." in help_text
