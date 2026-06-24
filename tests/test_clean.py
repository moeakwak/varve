from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from varve import Experiment, stage
from varve.cli.clean import _validate_destructive, clean
from varve.engine.runner import run
from varve.models import ProducedPath
from varve.store.lock import OutputLock
from varve.store.store import Store


class Config(BaseModel):
    pass


class CleanExperiment(Experiment):
    Config = Config

    @classmethod
    def default_output_root(cls, config: Config) -> Path:
        return Path("varve-test-output")

    @stage(produces="sample.txt")
    def sample(self, ctx):
        (ctx.out / "sample.txt").write_text("sample", encoding="utf-8")

    @stage(needs="sample", produces="summary.txt")
    def summarize(self, ctx):
        (ctx.out / "summary.txt").write_text("summary", encoding="utf-8")


class RestrictedCleanExperiment(CleanExperiment):
    @classmethod
    def clean_roots(cls, config: Config) -> list[Path] | None:
        return [Path("/tmp/varve-allowed-results")]


def test_validate_destructive_rejects_dangerous_paths(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _validate_destructive(Path("/"))
    with pytest.raises(ValueError):
        _validate_destructive(Path.home())
    with pytest.raises(ValueError):
        _validate_destructive(tmp_path, allowed_roots=[tmp_path / "other"])
    _validate_destructive(tmp_path / "out", allowed_roots=[tmp_path])


def test_clean_requires_manifest_anchor(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="Missing varve manifest"):
        clean(CleanExperiment, Config(), cli_out=tmp_path, yes=True)


def test_clean_full_output_root(tmp_path: Path) -> None:
    run(CleanExperiment, Config(), cli_out=tmp_path)
    clean(CleanExperiment, Config(), cli_out=tmp_path, yes=True, allowed_roots=[tmp_path.parent])
    assert not tmp_path.exists()


def test_clean_full_output_root_rejects_when_confirm_declines(tmp_path: Path) -> None:
    out = tmp_path / "out"
    run(CleanExperiment, Config(), cli_out=out)

    def rejecting_confirm(message: str) -> bool:
        assert str(out) in message
        return False

    with pytest.raises(ValueError, match="requires confirmation"):
        clean(CleanExperiment, Config(), cli_out=out, confirm=rejecting_confirm)
    assert out.exists()


def test_clean_full_output_root_accepts_when_confirm_accepts(tmp_path: Path) -> None:
    out = tmp_path / "out"
    run(CleanExperiment, Config(), cli_out=out)

    def accepting_confirm(message: str) -> bool:
        assert str(out) in message
        return True

    clean(CleanExperiment, Config(), cli_out=out, confirm=accepting_confirm)
    assert not out.exists()


def test_clean_yes_skips_confirm_callback(tmp_path: Path) -> None:
    out = tmp_path / "out"
    run(CleanExperiment, Config(), cli_out=out)

    def raising_confirm(message: str) -> bool:
        raise AssertionError(f"unexpected confirmation prompt: {message}")

    clean(CleanExperiment, Config(), cli_out=out, yes=True, confirm=raising_confirm)
    assert not out.exists()


def test_clean_target_keeps_upstream(tmp_path: Path) -> None:
    run(CleanExperiment, Config(), cli_out=tmp_path)
    clean(CleanExperiment, Config(), cli_out=tmp_path, target="summarize", yes=True)
    assert (tmp_path / "sample.txt").exists()
    assert not (tmp_path / "summary.txt").exists()


def test_clean_respects_output_lock(tmp_path: Path) -> None:
    run(CleanExperiment, Config(), cli_out=tmp_path)
    with OutputLock(Store(tmp_path).root):
        with pytest.raises(RuntimeError, match="already locked"):
            clean(CleanExperiment, Config(), cli_out=tmp_path, yes=True)


def test_clean_target_preflights_external_outputs(tmp_path: Path) -> None:
    run(CleanExperiment, Config(), cli_out=tmp_path)
    record = Store(tmp_path).read_success("summarize")
    assert record is not None
    assert record.produces is not None
    record.produces = [ProducedPath(path="/tmp/outside-varve.txt", kind="file")]
    Store(tmp_path).write_success(record)

    with pytest.raises(ValueError, match="outside root"):
        clean(CleanExperiment, Config(), cli_out=tmp_path, target="sample", yes=True)
    assert (tmp_path / "sample.txt").exists()


def test_default_clean_roots_does_not_restrict_full_clean(tmp_path: Path) -> None:
    out = tmp_path / "out"
    config = Config()
    run(CleanExperiment, config, cli_out=out)

    assert CleanExperiment.clean_roots(config) is None
    clean(
        CleanExperiment,
        config,
        cli_out=out,
        yes=True,
        allowed_roots=CleanExperiment.clean_roots(config),
    )
    assert not out.exists()


def test_cli_clean_full_output_root_rejects_eof_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "out"
    assert CleanExperiment.cli(["run", f"--out={out}"]) == 0
    prompted = False

    def eof_input(prompt: str) -> str:
        nonlocal prompted
        prompted = True
        assert str(out) in prompt
        assert "[y/N]" in prompt
        raise EOFError

    monkeypatch.setattr("builtins.input", eof_input)

    with pytest.raises(ValueError, match="requires confirmation"):
        CleanExperiment.cli(["clean", f"--out={out}"])
    assert prompted
    assert out.exists()


def test_cli_clean_full_output_root_rejects_empty_confirmation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "out"
    assert CleanExperiment.cli(["run", f"--out={out}"]) == 0
    prompted = False

    def empty_input(prompt: str) -> str:
        nonlocal prompted
        prompted = True
        assert str(out) in prompt
        assert "[y/N]" in prompt
        return ""

    monkeypatch.setattr("builtins.input", empty_input)

    with pytest.raises(ValueError, match="requires confirmation"):
        CleanExperiment.cli(["clean", f"--out={out}"])
    assert prompted
    assert out.exists()


def test_cli_clean_yes_skips_confirmation_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = tmp_path / "out"
    assert CleanExperiment.cli(["run", f"--out={out}"]) == 0

    def fail_input(prompt: str) -> str:
        raise AssertionError(f"unexpected confirmation prompt: {prompt}")

    monkeypatch.setattr("builtins.input", fail_input)

    assert CleanExperiment.cli(["clean", f"--out={out}", "--yes"]) == 0
    assert not out.exists()


def test_cli_clean_full_output_root_rejects_outside_clean_roots(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert RestrictedCleanExperiment.cli(["run", f"--out={out}"]) == 0

    with pytest.raises(ValueError, match="outside allowed roots"):
        RestrictedCleanExperiment.cli(["clean", f"--out={out}", "--yes"])
    assert out.exists()


def test_cli_clean_target_ignores_clean_roots_restriction(tmp_path: Path) -> None:
    out = tmp_path / "out"
    assert RestrictedCleanExperiment.cli(["run", f"--out={out}"]) == 0

    assert RestrictedCleanExperiment.cli(["clean", f"--out={out}", "summarize", "--yes"]) == 0
    assert out.exists()
    assert (out / "sample.txt").exists()
    assert not (out / "summary.txt").exists()
