from __future__ import annotations

from pathlib import Path

import pytest

from varve.dashboard.discovery import discover_experiments
from varve.store.store import Store


def test_discover_experiments_reads_manifest_and_dotifies_relative_paths(
    tmp_path: Path,
) -> None:
    Store(tmp_path).ensure_initialized("RootExperiment")
    Store(tmp_path / "simple").ensure_initialized("SimpleExperiment")
    Store(tmp_path / "nested" / "child").ensure_initialized("NestedExperiment")

    entries = discover_experiments(tmp_path)

    by_id = {entry.experiment_id: entry for entry in entries}
    assert by_id[tmp_path.name].output_root == tmp_path
    assert by_id[tmp_path.name].experiment_name == "RootExperiment"
    assert by_id["simple"].output_root == tmp_path / "simple"
    assert by_id["simple"].experiment_name == "SimpleExperiment"
    assert by_id["nested.child"].output_root == tmp_path / "nested" / "child"
    assert by_id["nested.child"].experiment_name == "NestedExperiment"
    assert [entry.experiment_id for entry in entries] == sorted(by_id)


def test_discover_experiments_keeps_scanning_when_manifest_is_not_readable(
    tmp_path: Path,
) -> None:
    bad_json = tmp_path / "bad-json" / ".varve"
    bad_json.mkdir(parents=True)
    (bad_json / "manifest.json").write_text("{bad", encoding="utf-8")

    missing_field = tmp_path / "missing-field" / ".varve"
    missing_field.mkdir(parents=True)
    (missing_field / "manifest.json").write_text("{}", encoding="utf-8")

    Store(tmp_path / "good").ensure_initialized("GoodExperiment")

    entries = discover_experiments(tmp_path)

    by_id = {entry.experiment_id: entry for entry in entries}
    assert by_id["bad-json"].experiment_name is None
    assert by_id["missing-field"].experiment_name is None
    assert by_id["good"].experiment_name == "GoodExperiment"


def test_discover_experiments_returns_empty_for_missing_root(tmp_path: Path) -> None:
    assert discover_experiments(tmp_path / "missing") == []


def test_discover_experiments_uses_output_root_name_when_scan_root_is_current_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    Store(tmp_path).ensure_initialized("Demo")
    monkeypatch.chdir(tmp_path)

    entries = discover_experiments(Path("."))

    assert [entry.experiment_id for entry in entries] == [tmp_path.name]


def test_discover_experiments_treats_manifest_schema_errors_as_unreadable(
    tmp_path: Path,
) -> None:
    manifest_dir = tmp_path / "bad-schema" / ".varve"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text(
        '{"experiment": "Demo", "extra": true}',
        encoding="utf-8",
    )

    entries = discover_experiments(tmp_path)

    assert entries[0].experiment_id == "bad-schema"
    assert entries[0].experiment_name is None
