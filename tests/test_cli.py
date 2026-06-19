from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from varve import Experiment, stage


class Config(BaseModel):
    out: Path
    token: str = "a"
    enabled: bool = True
    threshold: float = 0.0
    items: list[int] = Field(default_factory=list)


class InnerConfig(BaseModel):
    name: str = "inner"
    age: int = 0
    enabled: bool = True


class NestedConfig(Config):
    inner: InnerConfig = Field(default_factory=InnerConfig)


class CliExperiment(Experiment):
    Config = Config

    @stage(produces="sample.txt", key=["token"])
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(ctx.config.token, encoding="utf-8")


class NestedCliExperiment(CliExperiment):
    Config = NestedConfig


class ConflictingCliConfig(Config):
    target: str = "default-target"
    force: bool = False


class StringForceConfig(Config):
    force: str = "config-default"


class ConflictingCliExperiment(Experiment):
    Config = ConflictingCliConfig

    @stage(produces="first.txt", key=["target", "force"])
    def first(self, ctx):
        (ctx.out / "first.txt").write_text(
            f"{ctx.config.target}:{ctx.config.force}",
            encoding="utf-8",
        )

    @stage(produces="sample.txt", needs="first", key=["target", "force"])
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(
            f"{ctx.config.target}:{ctx.config.force}",
            encoding="utf-8",
        )


class StringForceCliExperiment(Experiment):
    Config = StringForceConfig

    @stage(produces="sample.txt", key=["force"])
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text(ctx.config.force, encoding="utf-8")


class UnsupportedConfig(BaseModel):
    out: Path
    extra: dict = Field(default_factory=dict)


class UnsupportedConfigExperiment(Experiment):
    Config = UnsupportedConfig

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text("sample", encoding="utf-8")


def test_cli_list_and_plan(capsys) -> None:
    assert CliExperiment.cli(["list"]) == 0
    assert "sample" in capsys.readouterr().out
    assert CliExperiment.cli(["plan"]) == 0
    assert "sample" in capsys.readouterr().out


def test_cli_run_status_clean(tmp_path: Path, capsys) -> None:
    assert CliExperiment.cli(["run", "--out", str(tmp_path), "--token", "x", "--no-enabled", "--items", "[1,2]"]) == 0
    assert (tmp_path / "sample.txt").read_text(encoding="utf-8") == "x"
    assert "no-cache" in capsys.readouterr().out

    assert CliExperiment.cli(["status", "--out", str(tmp_path), "--token", "x"]) == 0
    assert "hit" in capsys.readouterr().out

    assert CliExperiment.cli(["clean", "--out", str(tmp_path), "--yes"]) == 0
    assert not tmp_path.exists()


def test_cli_reads_yaml_config(tmp_path: Path, capsys) -> None:
    config_path = tmp_path / "config.yaml"
    out = tmp_path / "out"
    config_path.write_text(f"out: {out}\ntoken: yaml\n", encoding="utf-8")
    assert CliExperiment.cli(["run", "--config", str(config_path)]) == 0
    assert (out / "sample.txt").read_text(encoding="utf-8") == "yaml"
    assert "no-cache" in capsys.readouterr().out


def test_cli_plan_target_filters_graph(capsys) -> None:
    assert CliExperiment.cli(["plan", "sample"]) == 0
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
    [("run", "--out"), ("status", "--config"), ("clean", "--config")],
)
def test_cli_option_like_missing_value_does_not_trigger_config_argmap(
    command: str, value_option: str
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        UnsupportedConfigExperiment.cli([command, value_option, "--bogus"])
    assert exc_info.value.code != 0


@pytest.mark.parametrize("command", ["run", "status", "clean"])
def test_cli_config_commands_with_unsupported_config_still_fast_fail(command: str) -> None:
    with pytest.raises(TypeError, match="argmap does not support config field"):
        UnsupportedConfigExperiment.cli([command])


def test_cli_clean_target_after_equals_option_does_not_clean_root(tmp_path: Path) -> None:
    out = tmp_path / "out"
    config_path = tmp_path / "config.yaml"
    config_path.write_text(f"out: {out}\n", encoding="utf-8")
    assert CliExperiment.cli(["run", "--config", str(config_path)]) == 0
    extra = out / "extra.txt"
    extra.write_text("extra", encoding="utf-8")

    assert CliExperiment.cli(["clean", f"--config={config_path}", "sample", "--yes"]) == 0
    assert out.exists()
    assert extra.exists()
    assert not (out / "sample.txt").exists()


def test_cli_clean_target_after_dynamic_config_options_does_not_clean_root(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert CliExperiment.cli(["run", f"--out={out}"]) == 0
    extra = out / "extra.txt"
    extra.write_text("extra", encoding="utf-8")

    assert CliExperiment.cli(["clean", f"--out={out}", "--no-enabled", "sample", "--yes"]) == 0
    assert out.exists()
    assert extra.exists()
    assert not (out / "sample.txt").exists()


def test_cli_clean_target_after_nested_equals_option_does_not_clean_root(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert NestedCliExperiment.cli(["run", f"--out={out}", "--inner.name=custom"]) == 0
    extra = out / "extra.txt"
    extra.write_text("extra", encoding="utf-8")

    assert NestedCliExperiment.cli(
        ["clean", f"--out={out}", "--inner.name=custom", "sample", "--yes"]
    ) == 0
    assert out.exists()
    assert extra.exists()
    assert not (out / "sample.txt").exists()


def test_cli_clean_target_after_nested_bool_option_does_not_clean_root(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert NestedCliExperiment.cli(["run", f"--out={out}"]) == 0
    extra = out / "extra.txt"
    extra.write_text("extra", encoding="utf-8")

    assert NestedCliExperiment.cli(["clean", f"--out={out}", "--no-inner.enabled", "sample", "--yes"]) == 0
    assert out.exists()
    assert extra.exists()
    assert not (out / "sample.txt").exists()


def test_cli_command_args_do_not_pollute_conflicting_config_fields(tmp_path: Path) -> None:
    out = tmp_path / "out"

    assert ConflictingCliExperiment.cli(["run", "sample", f"--out={out}", "--force"]) == 0
    assert (out / "first.txt").read_text(encoding="utf-8") == "default-target:False"

    assert ConflictingCliExperiment.cli(
        ["run", "sample", f"--out={out}", "--target", "config"]
    ) == 0
    assert (out / "first.txt").read_text(encoding="utf-8") == "config:False"


def test_cli_accepts_negative_config_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Config] = []

    def fake_run(experiment, config, **kwargs):
        captured.append(config)
        return []

    monkeypatch.setattr("varve.cli.app.run", fake_run)
    assert CliExperiment.cli(["run", f"--out={tmp_path}", "--threshold", "-1"]) == 0

    assert captured[-1].threshold == -1


def test_cli_command_option_wins_when_config_field_has_same_name(tmp_path: Path) -> None:
    assert StringForceCliExperiment.cli(["run", f"--out={tmp_path}", "--force"]) == 0
    assert (tmp_path / "sample.txt").read_text(encoding="utf-8") == "config-default"


def test_cli_rejects_unknown_options_under_strict_argparse(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert CliExperiment.cli(["run", f"--out={out}"]) == 0
    extra = out / "extra.txt"
    extra.write_text("extra", encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        CliExperiment.cli(["clean", f"--out={out}", "--unknown", "sample", "--yes"])
    assert exc_info.value.code != 0
    assert out.exists()
    assert extra.exists()
    assert (out / "sample.txt").exists()


def test_cli_uses_sys_argv_when_argv_is_none(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["CliExperiment", "run", "--out", str(tmp_path)])
    assert CliExperiment.cli() == 0
    assert (tmp_path / "sample.txt").exists()


def test_cli_preserves_default_factory(tmp_path: Path) -> None:
    assert CliExperiment.cli(["run", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "sample.txt").exists()


def test_cli_end_to_end_equivalence_with_nested_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[NestedConfig] = []

    def fake_run(experiment, config, **kwargs):
        captured.append(config)
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
    assert captured[-1] == NestedConfig(
        out=out,
        enabled=False,
        items=[1, 2],
        inner=InnerConfig(name="cli", age=3),
    )

    config_path = tmp_path / "config.yaml"
    yaml_out = tmp_path / "yaml-out"
    config_path.write_text(
        f"out: {yaml_out}\ntoken: yaml\nitems: [5]\ninner:\n  name: yaml\n  age: 7\n",
        encoding="utf-8",
    )

    assert NestedCliExperiment.cli(
        ["run", "--config", str(config_path), "--inner.name", "cli"]
    ) == 0
    assert captured[-1] == NestedConfig(
        out=yaml_out,
        token="yaml",
        items=[5],
        inner=InnerConfig(name="cli", age=7),
    )


def test_cli_priority_cli_gt_env_gt_dotenv_gt_yaml_gt_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured: list[NestedConfig] = []

    def fake_run(experiment, config, **kwargs):
        captured.append(config)
        return []

    monkeypatch.setattr("varve.cli.app.run", fake_run)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "TOKEN=from-dotenv\nINNER__AGE=2\nINNER__NAME=from-dotenv\n",
        encoding="utf-8",
    )
    config_path = tmp_path / "cfg.yaml"
    config_path.write_text(
        "\n".join(
            [
                f"out: {tmp_path / 'out'}",
                "token: from-yaml",
                "items: [4, 5]",
                "inner:",
                "  name: from-yaml",
                "  age: 1",
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("TOKEN", "from-env")
    monkeypatch.setenv("INNER__AGE", "3")

    assert NestedCliExperiment.cli(["run", "--config", str(config_path), "--token", "from-cli"]) == 0

    config = captured[-1]
    assert config.token == "from-cli"
    assert config.inner.age == 3
    assert config.inner.name == "from-dotenv"
    assert config.items == [4, 5]
    assert config.enabled is True
