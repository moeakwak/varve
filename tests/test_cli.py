from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Literal

import pytest
from pydantic import BaseModel, Field, ValidationError

from varve import Experiment, stage
from varve.store.store import Store


class Config(BaseModel):
    token: str = "a"


class Args(BaseModel):
    enabled: bool = True
    threshold: float = 0.0
    items: list[int] = Field(default_factory=list)


class InnerArgs(BaseModel):
    name: str = "inner"
    age: int = 0
    enabled: bool = True


class NestedArgs(Args):
    inner: InnerArgs = Field(default_factory=InnerArgs)


class CliExperiment(Experiment):
    Args = Args
    Config = Config

    @classmethod
    def default_output_root(cls, config: Config) -> Path:
        return Path("varve-test-output")

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(ctx.config.token, encoding="utf-8")


class NestedCliExperiment(CliExperiment):
    Args = NestedArgs


class ConflictingCliConfig(Config):
    target: str = "default-target"
    force: bool = False


class ConflictingCliArgs(BaseModel):
    target: str = "default-target"
    force: bool = False


class StringForceArgs(BaseModel):
    force: str = "config-default"


class ConflictingCliExperiment(Experiment):
    Args = ConflictingCliArgs
    Config = ConflictingCliConfig

    @stage(produces="first.txt")
    def first(self, ctx):
        (ctx.out / "first.txt").write_text(
            f"{ctx.args.target}:{ctx.args.force}",
            encoding="utf-8",
        )

    @stage(produces="sample.txt", needs="first")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(
            f"{ctx.args.target}:{ctx.args.force}",
            encoding="utf-8",
        )


class StringForceCliExperiment(Experiment):
    Args = StringForceArgs
    Config = Config

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(ctx.args.force, encoding="utf-8")


class UnsupportedConfig(BaseModel):
    extra: dict = Field(default_factory=dict)


class UnsupportedConfigExperiment(Experiment):
    Args = UnsupportedConfig
    Config = UnsupportedConfig

    @classmethod
    def default_output_root(cls, config: UnsupportedConfig) -> Path:
        return Path("varve-test-output")

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text("sample", encoding="utf-8")


def test_cli_list_and_plan(capsys) -> None:
    assert CliExperiment.cli(["list"]) == 0
    assert "sample" in capsys.readouterr().out
    assert CliExperiment.cli(["plan"]) == 0
    assert "sample" in capsys.readouterr().out


def test_cli_run_status_clean(tmp_path: Path, capsys) -> None:
    override = '{"token":"x"}'
    assert CliExperiment.cli(
        [
            "run",
            "--out",
            str(tmp_path),
            "--branch",
            "quick",
            "--override",
            override,
            "--no-enabled",
            "--items",
            "[1,2]",
        ]
    ) == 0
    assert (tmp_path / ".tmp" / "quick" / "sample.txt").read_text(encoding="utf-8") == "x"
    assert "no-cache" in capsys.readouterr().out

    assert CliExperiment.cli(["status", "--out", str(tmp_path), "--branch", "quick"]) == 0
    assert "hit" in capsys.readouterr().out

    assert CliExperiment.cli(["clean", "--out", str(tmp_path), "--branch", "quick", "--yes"]) == 0
    assert not (tmp_path / ".tmp" / "quick").exists()


def test_cli_reads_yaml_config(tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_path / "config.yaml"
    out = tmp_path / "out"
    config_path.write_text("main:\n  token: yaml\n", encoding="utf-8")
    monkeypatch.setattr(
        CliExperiment,
        "varve_config_path",
        classmethod(lambda cls: config_path),
    )
    assert CliExperiment.cli(["run", "--out", str(out)]) == 0
    assert (out / "main" / "sample.txt").read_text(encoding="utf-8") == "yaml"
    assert "no-cache" in capsys.readouterr().out


def test_cli_plan_target_filters_graph(capsys) -> None:
    assert CliExperiment.cli(["plan", "--upto", "sample"]) == 0
    assert capsys.readouterr().out.strip() == "sample"


def test_cli_list_and_plan_do_not_require_supported_config(capsys) -> None:
    assert UnsupportedConfigExperiment.cli(["list"]) == 0
    assert "sample" in capsys.readouterr().out
    assert UnsupportedConfigExperiment.cli(["plan"]) == 0
    assert capsys.readouterr().out.strip() == "sample"


@pytest.mark.parametrize("known_command", ["run", "status", "clean"])
def test_cli_unknown_command_does_not_trigger_config_argmap(known_command: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        UnsupportedConfigExperiment.cli(["bogus", known_command])
    assert exc_info.value.code != 0


@pytest.mark.parametrize("command", ["run", "status", "clean"])
def test_cli_unknown_option_does_not_trigger_config_argmap(command: str) -> None:
    with pytest.raises(SystemExit) as exc_info:
        UnsupportedConfigExperiment.cli([command, "--bogus"])
    assert exc_info.value.code != 0


@pytest.mark.parametrize(
    ("command", "value_option"),
    [("run", "--out"), ("status", "--out"), ("clean", "--out")],
)
def test_cli_option_like_missing_value_does_not_trigger_config_argmap(
    command: str, value_option: str
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        UnsupportedConfigExperiment.cli([command, value_option, "--bogus"])
    assert exc_info.value.code != 0


@pytest.mark.parametrize("command", ["run", "status", "clean"])
def test_cli_config_commands_with_unsupported_args_still_fast_fail(command: str) -> None:
    with pytest.raises(TypeError, match="argmap does not support args field"):
        UnsupportedConfigExperiment.cli([command])


def test_cli_clean_target_after_equals_option_does_not_clean_root(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert CliExperiment.cli(["run", f"--out={out}"]) == 0
    root = out / "main"
    extra = root / "extra.txt"
    extra.write_text("extra", encoding="utf-8")

    assert CliExperiment.cli(["clean", f"--out={out}", "--downstream", "sample", "--yes"]) == 0
    assert root.exists()
    assert extra.exists()
    assert not (root / "sample.txt").exists()


def test_cli_clean_target_after_dynamic_config_options_does_not_clean_root(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert CliExperiment.cli(["run", f"--out={out}"]) == 0
    root = out / "main"
    extra = root / "extra.txt"
    extra.write_text("extra", encoding="utf-8")

    assert CliExperiment.cli(
        ["clean", f"--out={out}", "--no-enabled", "--downstream", "sample", "--yes"]
    ) == 0
    assert root.exists()
    assert extra.exists()
    assert not (root / "sample.txt").exists()


def test_cli_clean_target_after_nested_equals_option_does_not_clean_root(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert NestedCliExperiment.cli(["run", f"--out={out}", "--inner.name=custom"]) == 0
    root = out / "main"
    extra = root / "extra.txt"
    extra.write_text("extra", encoding="utf-8")

    assert NestedCliExperiment.cli(
        ["clean", f"--out={out}", "--inner.name=custom", "--downstream", "sample", "--yes"]
    ) == 0
    assert root.exists()
    assert extra.exists()
    assert not (root / "sample.txt").exists()


def test_cli_clean_target_after_nested_bool_option_does_not_clean_root(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert NestedCliExperiment.cli(["run", f"--out={out}"]) == 0
    root = out / "main"
    extra = root / "extra.txt"
    extra.write_text("extra", encoding="utf-8")

    assert NestedCliExperiment.cli(
        ["clean", f"--out={out}", "--no-inner.enabled", "--downstream", "sample", "--yes"]
    ) == 0
    assert root.exists()
    assert extra.exists()
    assert not (root / "sample.txt").exists()


def test_cli_command_args_do_not_pollute_conflicting_args_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "out"
    captured: list[ConflictingCliArgs] = []

    def fake_run(experiment, config, **kwargs):
        captured.append(kwargs["args"])
        return []

    monkeypatch.setattr("varve.cli.app.run", fake_run)
    assert ConflictingCliExperiment.cli(["run", "--upto", "sample", f"--out={out}", "--force"]) == 0
    assert captured[-1] == ConflictingCliArgs()

    assert ConflictingCliExperiment.cli(
        ["run", "--upto", "sample", f"--out={out}", "--target", "config"]
    ) == 0
    assert captured[-1] == ConflictingCliArgs(target="config")


def test_cli_accepts_negative_config_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Args] = []

    def fake_run(experiment, config, **kwargs):
        captured.append(kwargs["args"])
        return []

    monkeypatch.setattr("varve.cli.app.run", fake_run)
    assert CliExperiment.cli(["run", f"--out={tmp_path}", "--threshold", "-1"]) == 0

    assert captured[-1].threshold == -1


def test_cli_passes_out_as_builtin_runner_argument(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[Config, dict]] = []

    def fake_run(experiment, config, **kwargs):
        captured.append((config, kwargs))
        return []

    monkeypatch.setattr("varve.cli.app.run", fake_run)
    assert CliExperiment.cli(["run", f"--out={tmp_path}"]) == 0

    config, kwargs = captured[-1]
    assert not hasattr(config, "out")
    assert kwargs["cli_out"] == tmp_path
    assert kwargs["branch"] == "main"


def test_cli_command_option_wins_when_config_field_has_same_name(tmp_path: Path) -> None:
    assert StringForceCliExperiment.cli(["run", f"--out={tmp_path}", "--force"]) == 0
    assert (tmp_path / "main" / "sample.txt").read_text(encoding="utf-8") == "config-default"


def test_cli_rejects_unknown_options_under_strict_argparse(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert CliExperiment.cli(["run", f"--out={out}"]) == 0
    root = out / "main"
    extra = root / "extra.txt"
    extra.write_text("extra", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        CliExperiment.cli(["clean", f"--out={out}", "--unknown", "--downstream", "sample", "--yes"])
    assert exc_info.value.code != 0
    assert root.exists()
    assert extra.exists()
    assert (root / "sample.txt").exists()


def test_cli_uses_sys_argv_when_argv_is_none(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["CliExperiment", "run", "--out", str(tmp_path)])
    assert CliExperiment.cli() == 0
    assert (tmp_path / "main" / "sample.txt").exists()


def test_cli_preserves_default_factory(tmp_path: Path) -> None:
    assert CliExperiment.cli(["run", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "main" / "sample.txt").exists()


def test_cli_end_to_end_equivalence_with_nested_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[NestedArgs] = []

    def fake_run(experiment, config, **kwargs):
        captured.append(kwargs["args"])
        return []

    monkeypatch.setattr("varve.cli.app.run", fake_run)

    out = tmp_path / "out"
    assert NestedCliExperiment.cli(
        [
            "run",
            f"--out={out}",
            "--inner.name=cli",
            "--inner.age=3",
            "--enabled",
            "--no-enabled",
            "--items",
            "[1,2]",
        ]
    ) == 0
    assert captured[-1] == NestedArgs(
        enabled=False,
        items=[1, 2],
        inner=InnerArgs(name="cli", age=3),
    )

    config_path = tmp_path / "config.yaml"
    yaml_out = tmp_path / "yaml-out"
    config_path.write_text(
        "main:\n  token: yaml\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        NestedCliExperiment,
        "varve_config_path",
        classmethod(lambda cls: config_path),
    )
    assert NestedCliExperiment.cli(["run", f"--out={yaml_out}", "--inner.name", "cli"]) == 0
    assert captured[-1] == NestedArgs(inner=InnerArgs(name="cli"))


def test_cli_priority_cli_gt_env_gt_dotenv_gt_yaml_gt_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[tuple[Config, NestedArgs]] = []

    def fake_run(experiment, config, **kwargs):
        captured.append((config, kwargs["args"]))
        return []

    monkeypatch.setattr("varve.cli.app.run", fake_run)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "TOKEN=from-dotenv\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "cfg.yaml"
    config_path.write_text("main: {}\n", encoding="utf-8")
    monkeypatch.setenv("TOKEN", "from-env")
    monkeypatch.setattr(
        NestedCliExperiment,
        "varve_config_path",
        classmethod(lambda cls: config_path),
    )

    assert NestedCliExperiment.cli(["run", "--inner.age", "3"]) == 0

    config, args = captured[-1]
    assert config.token == "from-env"
    assert args.inner.age == 3
    assert args.inner.name == "inner"
    assert args.items == []
    assert args.enabled is True


class Engine(str, Enum):
    mathjax = "mathjax"
    katex = "katex"


class ChoiceArgs(BaseModel):
    mode: Literal["fast", "slow"] = "fast"
    engine: Engine = Engine.mathjax
    sample: int | None = 5


class ChoiceCliExperiment(Experiment):
    Args = ChoiceArgs
    Config = Config

    @classmethod
    def default_output_root(cls, config: Config) -> Path:
        return Path("varve-test-output")

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(ctx.args.mode, encoding="utf-8")


class RequiredExtraConfig(BaseModel):
    dataset: str


class RequiredExtraCliExperiment(Experiment):
    Config = RequiredExtraConfig

    @classmethod
    def default_output_root(cls, config: RequiredExtraConfig) -> Path:
        return Path("varve-test-output")

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(ctx.config.dataset, encoding="utf-8")


class StrictCleanRootsExperiment(RequiredExtraCliExperiment):
    @classmethod
    def clean_roots(cls, config: RequiredExtraConfig) -> list[Path] | None:
        return [Path("/tmp") / config.dataset]


def _capture_run(monkeypatch: pytest.MonkeyPatch) -> list[tuple[BaseModel, BaseModel, dict]]:
    captured: list = []

    def fake_run(experiment, config, **kwargs):
        captured.append((config, kwargs["args"], kwargs))
        return []

    monkeypatch.setattr("varve.cli.app.run", fake_run)
    return captured


def test_cli_literal_field_accepts_valid_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _capture_run(monkeypatch)
    assert ChoiceCliExperiment.cli(["run", f"--out={tmp_path}", "--mode", "slow"]) == 0
    assert captured[-1][1].mode == "slow"


def test_cli_literal_field_rejects_invalid_choice(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        ChoiceCliExperiment.cli(["run", f"--out={tmp_path}", "--mode", "bogus"])
    assert exc_info.value.code != 0


def test_cli_str_enum_field_accepts_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _capture_run(monkeypatch)
    assert ChoiceCliExperiment.cli(["run", f"--out={tmp_path}", "--engine", "katex"]) == 0
    assert captured[-1][1].engine is Engine.katex


def test_cli_str_enum_field_rejects_invalid_value(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exc_info:
        ChoiceCliExperiment.cli(["run", f"--out={tmp_path}", "--engine", "bogus"])
    assert exc_info.value.code != 0


def test_cli_optional_field_accepts_null_sentinel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _capture_run(monkeypatch)
    assert ChoiceCliExperiment.cli(["run", f"--out={tmp_path}", "--sample", "null"]) == 0
    assert captured[-1][1].sample is None


def test_cli_optional_field_accepts_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _capture_run(monkeypatch)
    assert ChoiceCliExperiment.cli(["run", f"--out={tmp_path}", "--sample", "9"]) == 0
    assert captured[-1][1].sample == 9


@pytest.mark.parametrize("command", ["run", "status", "clean"])
def test_cli_help_is_handled_by_argparse(command: str, capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        ChoiceCliExperiment.cli([command, "--help"])
    assert exc_info.value.code == 0
    assert "--mode" in capsys.readouterr().out


def test_cli_help_hides_removed_flags_and_internal_dest(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        NestedCliExperiment.cli(["run", "--help"])
    assert exc_info.value.code == 0
    help_text = capsys.readouterr().out
    assert "__VARVE_CONFIG__" not in help_text
    assert "__VARVE_ARGS__" not in help_text
    for removed in ["--only", "--dry", "--config", "--name"]:
        assert removed not in help_text
    assert "--upto STAGE" in help_text
    assert "--downstream STAGE" in help_text


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "sample"],
        ["run", "--only", "sample"],
        ["run", "--dry"],
        ["run", "--config", "cfg.yaml"],
        ["run", "--name", "quick"],
        ["status", "--override", "{}"],
        ["clean", "--override", "{}", "--yes"],
        ["plan", "--mermaid"],
        ["plan", "--dot"],
    ],
)
def test_cli_rejects_removed_surface(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        CliExperiment.cli(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["run", "--upto", "sample", "--downstream", "sample"],
        ["status", "--upto", "sample", "--downstream", "sample"],
        ["plan", "--upto", "sample", "--downstream", "sample"],
    ],
)
def test_cli_rejects_mutually_exclusive_stage_filters(argv: list[str]) -> None:
    with pytest.raises(SystemExit):
        CliExperiment.cli(argv)


def test_cli_clean_with_bare_output_root_skips_required_fields(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert RequiredExtraCliExperiment.cli(
        ["run", f"--out={out}", "--branch", "alpha", "--override", '{"dataset":"alpha"}']
    ) == 0
    assert (out / ".tmp" / "alpha" / "sample.txt").exists()

    assert RequiredExtraCliExperiment.cli(["status", f"--out={out}", "--branch", "alpha"]) == 0

    # clean only needs the output root, not the unrelated required `dataset` field.
    assert RequiredExtraCliExperiment.cli(
        ["clean", f"--out={out}", "--branch", "alpha", "--yes"]
    ) == 0
    assert not (out / ".tmp" / "alpha").exists()


def test_cli_named_override_branch_reuses_manifest_and_guards_config(
    tmp_path: Path, capsys
) -> None:
    out = tmp_path / "out"
    argv = ["run", f"--out={out}", "--branch", "quick", "--override", '{"token":"x"}']
    assert CliExperiment.cli(argv) == 0
    assert (out / ".tmp" / "quick" / "sample.txt").read_text(encoding="utf-8") == "x"

    assert CliExperiment.cli(argv) == 0
    assert CliExperiment.cli(["status", f"--out={out}", "--branch", "quick"]) == 0
    assert "hit" in capsys.readouterr().out

    with pytest.raises(ValueError, match="different config"):
        CliExperiment.cli(["run", f"--out={out}", "--branch", "quick", "--override", '{"token":"y"}'])


def test_cli_hash_override_branch_uses_validated_config_not_json_order(
    tmp_path: Path,
) -> None:
    out = tmp_path / "out"
    assert CliExperiment.cli(["run", f"--out={out}", "--override", '{"token":"x"}']) == 0
    first = sorted((out / ".tmp").iterdir())
    assert len(first) == 1
    assert first[0].name.startswith("main_override_")

    assert CliExperiment.cli(["run", f"--out={out}", "--override", '{ "token" : "x" }']) == 0
    second = sorted((out / ".tmp").iterdir())
    assert [path.name for path in second] == [first[0].name]


def test_cli_status_and_clean_locate_named_override_without_override(
    tmp_path: Path,
) -> None:
    out = tmp_path / "out"
    assert CliExperiment.cli(
        ["run", f"--out={out}", "--branch", "quick", "--override", '{"token":"x"}']
    ) == 0
    assert CliExperiment.cli(["clean", f"--out={out}", "--branch", "quick", "--yes"]) == 0
    assert not (out / ".tmp" / "quick").exists()


def test_cli_rejects_yaml_branch_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path = tmp_path / "varve.yaml"
    config_path.write_text("alt:\n  token: alt\n", encoding="utf-8")
    monkeypatch.setattr(
        CliExperiment,
        "varve_config_path",
        classmethod(lambda cls: config_path),
    )

    with pytest.raises(ValueError, match="only supported on main"):
        CliExperiment.cli(["run", "--branch", "alt", "--override", '{"token":"x"}'])


def test_cli_yaml_branch_does_not_require_valid_main_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "out"
    config_path = tmp_path / "varve.yaml"
    config_path.write_text("main: {}\nalt:\n  dataset: beta\n", encoding="utf-8")
    monkeypatch.setattr(
        RequiredExtraCliExperiment,
        "varve_config_path",
        classmethod(lambda cls: config_path),
    )

    assert RequiredExtraCliExperiment.cli(["run", f"--out={out}", "--branch", "alt"]) == 0
    assert (out / "alt" / "sample.txt").read_text(encoding="utf-8") == "beta"


def test_cli_old_temporary_manifest_cannot_be_reused(tmp_path: Path) -> None:
    manifest = tmp_path / "out" / ".tmp" / "legacy" / ".varve" / "manifest.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"schema_version":1,"experiment":"CliExperiment"}\n', encoding="utf-8")

    with pytest.raises(ValueError, match="old manifest"):
        CliExperiment.cli(["status", f"--out={tmp_path / 'out'}", "--branch", "legacy"])


def test_cli_clean_without_out_requires_full_config() -> None:
    with pytest.raises(ValidationError):
        RequiredExtraCliExperiment.cli(["clean", "--yes"])


def test_cli_clean_with_bare_output_root_skips_clean_roots_config_access(tmp_path: Path) -> None:
    out = tmp_path / "out"
    Store(out / "main").ensure_initialized("StrictCleanRootsExperiment")

    assert StrictCleanRootsExperiment.cli(["clean", f"--out={out}", "--yes"]) == 0

    assert not (out / "main").exists()
