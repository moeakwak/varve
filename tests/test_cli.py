from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from varve import Experiment, stage


class Config(BaseModel):
    out: Path
    token: str = "a"
    enabled: bool = True
    items: list[int] = Field(default_factory=list)


class InnerConfig(BaseModel):
    name: str = "inner"
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


def test_cli_clean_target_after_unknown_option_does_not_clean_root(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert CliExperiment.cli(["run", f"--out={out}"]) == 0
    extra = out / "extra.txt"
    extra.write_text("extra", encoding="utf-8")

    assert CliExperiment.cli(["clean", f"--out={out}", "--unknown", "sample", "--yes"]) == 0
    assert out.exists()
    assert extra.exists()
    assert not (out / "sample.txt").exists()


def test_cli_uses_sys_argv_when_argv_is_none(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["CliExperiment", "run", "--out", str(tmp_path)])
    assert CliExperiment.cli() == 0
    assert (tmp_path / "sample.txt").exists()


def test_cli_preserves_default_factory(tmp_path: Path) -> None:
    assert CliExperiment.cli(["run", "--out", str(tmp_path)]) == 0
    assert (tmp_path / "sample.txt").exists()
