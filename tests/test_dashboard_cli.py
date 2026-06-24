from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from varve.dashboard import render
from varve.dashboard.cli import main
from varve.dashboard.models import ExperimentEntry, ExperimentState
from varve.dashboard.render import render_detail
from varve.models import KeyComponents, ProducedPath, SuccessRecord
from varve.store.store import Store


def _components(upstreams: dict[str, dict[str, str]] | None = None) -> KeyComponents:
    return KeyComponents(source={}, config={}, files={}, values={}, upstreams=upstreams or {})


def _success(
    stage: str,
    *,
    upstreams: dict[str, dict[str, str]] | None = None,
    committed_at: str = "2026-06-24T10:00:00+00:00",
) -> SuccessRecord:
    return SuccessRecord(
        experiment="Demo",
        stage=stage,
        kind="single",
        content_key=f"sha256:{stage}",
        key_components=_components(upstreams),
        produces=[ProducedPath(path=f"{stage}.txt", kind="file")],
        committed_at=committed_at,
    )


def _write_stage(output_root: Path, stage: str, **kwargs) -> None:
    (output_root / f"{stage}.txt").write_text(stage, encoding="utf-8")
    Store(output_root).write_success(_success(stage, **kwargs))


def test_ls_and_show_render_experiment_state(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    output_root = tmp_path / "alpha"
    Store(output_root).ensure_initialized("Demo")
    _write_stage(output_root, "sample")

    assert main(["ls", "--root", str(tmp_path)]) == 0
    ls = capsys.readouterr()
    assert "alpha" in ls.out
    assert "ok" in ls.out
    assert "1/1" in ls.out
    assert "2026-06-24 10:00" in ls.out

    assert main(["show", "alpha", "--root", str(tmp_path)]) == 0
    detail = capsys.readouterr()
    assert "alpha" in detail.out
    assert str(output_root) in detail.out
    assert "sample" in detail.out
    assert "sample.txt" in detail.out


def test_ls_returns_nonzero_for_empty_scan_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["ls", "--root", str(tmp_path / "missing")]) == 1

    captured = capsys.readouterr()
    assert "No experiments found" in captured.err
    assert captured.out == ""


def test_show_returns_nonzero_and_lists_known_ids_for_unknown_experiment(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    Store(tmp_path / "alpha").ensure_initialized("Alpha")
    Store(tmp_path / "beta").ensure_initialized("Beta")

    assert main(["show", "missing", "--root", str(tmp_path)]) == 1

    captured = capsys.readouterr()
    assert "Unknown experiment: missing" in captured.err
    assert "alpha" in captured.err
    assert "beta" in captured.err


def test_no_subcommand_defaults_to_ls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_root = tmp_path / "default"
    Store(output_root).ensure_initialized("Demo")
    _write_stage(output_root, "sample")
    monkeypatch.chdir(tmp_path)

    assert main([]) == 0

    captured = capsys.readouterr()
    assert "default" in captured.out


def test_show_plan_lists_real_edges_without_inventing_topological_links(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    output_root = tmp_path / "dag"
    Store(output_root).ensure_initialized("Demo")
    _write_stage(output_root, "a")
    _write_stage(output_root, "b", upstreams={"a": {"content_key": "sha256:a"}})
    _write_stage(output_root, "c", upstreams={"a": {"content_key": "sha256:a"}})
    _write_stage(output_root, "d")

    assert main(["show", "dag", "--root", str(tmp_path)]) == 0

    output = capsys.readouterr().out
    assert "a -> b" in output
    assert "a -> c" in output
    assert "b -> c" not in output
    assert "c -> d" not in output
    assert "d ->" not in output


def test_render_detail_styles_overall_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    buffer = StringIO()

    def console_factory(**kwargs):
        return Console(
            file=buffer,
            force_terminal=True,
            color_system="standard",
            no_color=False,
            width=120,
            **kwargs,
        )

    monkeypatch.setattr(render, "Console", console_factory)
    state = ExperimentState(
        entry=ExperimentEntry(
            output_root=tmp_path,
            experiment_id="demo",
            experiment_name="Demo",
        ),
        stages=[],
        order=[],
        overall="corrupt",
    )

    render_detail(state)

    assert "Overall: \x1b[31mcorrupt\x1b[0m" in buffer.getvalue()
