from __future__ import annotations

import json
from pathlib import Path

from varve.dashboard.discovery import discover_pipelines
from varve.store.store import Store


def test_discover_pipelines_requires_branch_output_layout(
    tmp_path: Path,
) -> None:
    Store(tmp_path).ensure_initialized("RootPipeline")
    Store(tmp_path / "simple").ensure_initialized("SimplePipeline")
    Store(tmp_path / "nested" / "child" / "out" / "main").ensure_initialized(
        "NestedPipeline",
        module="pkg.nested",
    )

    entries = discover_pipelines(tmp_path)

    by_id = {entry.pipeline_id: entry for entry in entries}
    assert set(by_id) == {"nested.child"}
    assert by_id["nested.child"].output_root == tmp_path / "nested" / "child" / "out" / "main"
    assert by_id["nested.child"].pipeline_name == "NestedPipeline"
    assert by_id["nested.child"].module == "pkg.nested"
    assert by_id["nested.child"].manifest_error is None
    assert by_id["nested.child"].branch == "main"


def test_discover_pipelines_splits_colocated_output_branch_and_filters_temporary(
    tmp_path: Path,
) -> None:
    Store(tmp_path / "analysis" / "demo" / "out" / "main").ensure_initialized("Demo")
    Store(tmp_path / "analysis" / "demo" / "out" / "exp1").ensure_initialized("Demo")
    Store(tmp_path / "analysis" / "demo" / "out" / ".tmp" / "quick").ensure_initialized("Demo")

    entries = discover_pipelines(tmp_path)

    by_key = {(entry.pipeline_id, entry.branch): entry for entry in entries}
    assert ("analysis.demo", "main") in by_key
    assert ("analysis.demo", "exp1") in by_key
    assert ("analysis.demo", "quick") not in by_key
    assert by_key[("analysis.demo", "main")].output_root == (
        tmp_path / "analysis" / "demo" / "out" / "main"
    )


def test_discover_pipelines_can_include_temporary_branches(
    tmp_path: Path,
) -> None:
    Store(tmp_path / "analysis" / "demo" / "out" / "main").ensure_initialized("Demo")
    Store(tmp_path / "analysis" / "demo" / "out" / ".tmp" / "quick").ensure_initialized("Demo")

    entries = discover_pipelines(tmp_path, include_temporary=True)

    by_key = {(entry.pipeline_id, entry.branch): entry for entry in entries}
    assert ("analysis.demo", "main") in by_key
    assert ("analysis.demo", "quick") in by_key
    assert by_key[("analysis.demo", "quick")].output_root == (
        tmp_path / "analysis" / "demo" / "out" / ".tmp" / "quick"
    )


def test_discover_pipelines_filters_temporary_when_scan_root_is_out_dir(
    tmp_path: Path,
) -> None:
    out = tmp_path / "demo" / "out"
    Store(out / "main").ensure_initialized("Demo")
    Store(out / ".tmp" / "quick").ensure_initialized("Demo")

    entries = discover_pipelines(out)

    assert [(entry.pipeline_id, entry.branch) for entry in entries] == [("demo", "main")]


def test_discover_pipelines_can_include_temporary_when_scan_root_is_out_dir(
    tmp_path: Path,
) -> None:
    out = tmp_path / "demo" / "out"
    Store(out / "main").ensure_initialized("Demo")
    Store(out / ".tmp" / "quick").ensure_initialized("Demo")

    entries = discover_pipelines(out, include_temporary=True)

    assert [(entry.pipeline_id, entry.branch) for entry in entries] == [
        ("demo", "main"),
        ("demo", "quick"),
    ]


def test_discover_pipelines_keeps_scanning_when_manifest_is_not_readable(
    tmp_path: Path,
) -> None:
    bad_json = tmp_path / "bad-json" / "out" / "main" / ".varve"
    bad_json.mkdir(parents=True)
    (bad_json / "manifest.json").write_text("{bad", encoding="utf-8")

    missing_field = tmp_path / "missing-field" / "out" / "main" / ".varve"
    missing_field.mkdir(parents=True)
    (missing_field / "manifest.json").write_text("{}", encoding="utf-8")

    Store(tmp_path / "good" / "out" / "main").ensure_initialized("GoodPipeline")

    entries = discover_pipelines(tmp_path)

    by_id = {entry.pipeline_id: entry for entry in entries}
    assert by_id["bad-json"].pipeline_name is None
    assert by_id["bad-json"].manifest_error
    assert by_id["missing-field"].pipeline_name is None
    assert by_id["missing-field"].manifest_error
    assert by_id["good"].pipeline_name == "GoodPipeline"


def test_discover_pipelines_keeps_manifest_without_module(
    tmp_path: Path,
) -> None:
    Store(tmp_path / "legacy" / "out" / "main").ensure_initialized("LegacyPipeline")

    entries = discover_pipelines(tmp_path)

    assert entries[0].pipeline_name == "LegacyPipeline"
    assert entries[0].module is None
    assert entries[0].manifest_error is None


def test_discover_pipelines_returns_empty_for_missing_root(tmp_path: Path) -> None:
    assert discover_pipelines(tmp_path / "missing") == []


def test_discover_pipelines_treats_manifest_schema_errors_as_unreadable(
    tmp_path: Path,
) -> None:
    manifest_dir = tmp_path / "bad-schema" / "out" / "main" / ".varve"
    manifest_dir.mkdir(parents=True)
    legacy_key = "ex" + "periment"
    (manifest_dir / "manifest.json").write_text(
        json.dumps({legacy_key: "Demo", "extra": True}),
        encoding="utf-8",
    )

    entries = discover_pipelines(tmp_path)

    assert entries[0].pipeline_id == "bad-schema"
    assert entries[0].branch == "main"
    assert entries[0].pipeline_name is None
    assert entries[0].manifest_error
