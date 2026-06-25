from __future__ import annotations

from pathlib import Path

from varve.dashboard.models import ExperimentEntry
from varve.dashboard.state import load_state
from varve.models import (
    AttemptMarker,
    KeyComponents,
    OutputHandle,
    ProducedPath,
    SuccessRecord,
)
from varve.store.store import Store


def _entry(output_root: Path, experiment_name: str | None = "Demo") -> ExperimentEntry:
    return ExperimentEntry(
        output_root=output_root,
        experiment_id=output_root.name,
        experiment_name=experiment_name,
        branch="main",
    )


def _components(upstreams: dict[str, dict[str, str]] | None = None) -> KeyComponents:
    return KeyComponents(
        source={},
        config={},
        files={},
        values={},
        upstreams=upstreams or {},
    )


def _single(
    stage: str,
    *,
    path: str | None = None,
    upstreams: dict[str, dict[str, str]] | None = None,
    committed_at: str = "2026-06-24T10:00:00+00:00",
) -> SuccessRecord:
    return SuccessRecord(
        experiment="Demo",
        stage=stage,
        kind="single",
        content_key=f"sha256:{stage}",
        key_components=_components(upstreams),
        produces=[ProducedPath(path=path or f"{stage}.txt", kind="file")],
        committed_at=committed_at,
    )


def _batch(
    stage: str,
    *,
    paths: list[str] | None = None,
    upstreams: dict[str, dict[str, str]] | None = None,
    committed_at: str = "2026-06-24T10:00:00+00:00",
) -> SuccessRecord:
    return SuccessRecord(
        experiment="Demo",
        stage=stage,
        kind="batch",
        content_key=f"sha256:{stage}",
        key_components=_components(upstreams),
        partition_values={"batch": 1},
        outputs=[
            OutputHandle(index=index, path=path)
            for index, path in enumerate(paths or [f"{stage}-0.txt", f"{stage}-1.txt"])
        ],
        committed_at=committed_at,
    )


def _attempt() -> AttemptMarker:
    return AttemptMarker(content_key="sha256:attempt", started_at="now", touched_existing=False)


def test_load_state_marks_single_artifacts_ok_or_missing(tmp_path: Path) -> None:
    store = Store(tmp_path)
    (tmp_path / "present.txt").write_text("ok", encoding="utf-8")
    store.write_success(_single("present"))
    store.write_success(_single("missing"))

    state = load_state(_entry(tmp_path))

    stages = {stage.name: stage for stage in state.stages}
    assert stages["present"].status == "ok"
    assert stages["present"].artifacts[0].path == Path("present.txt")
    assert stages["present"].artifacts[0].exists is True
    assert stages["missing"].status == "artifact-missing"
    assert stages["missing"].artifacts[0].exists is False
    assert state.overall == "artifact-missing"


def test_load_state_reads_batch_artifacts_from_outputs(tmp_path: Path) -> None:
    for name in ["part-0.txt", "part-1.txt"]:
        (tmp_path / name).write_text(name, encoding="utf-8")
    Store(tmp_path).write_success(_batch("batch", paths=["part-0.txt", "part-1.txt"]))

    state = load_state(_entry(tmp_path))

    stage = state.stages[0]
    assert stage.status == "ok"
    assert [artifact.path for artifact in stage.artifacts] == [
        Path("part-0.txt"),
        Path("part-1.txt"),
    ]


def test_load_state_includes_attempt_only_and_attempt_over_success_stages(
    tmp_path: Path,
) -> None:
    store = Store(tmp_path)
    store.write_attempt("attempt-only", _attempt())
    (tmp_path / "dirty.txt").write_text("ok", encoding="utf-8")
    store.write_success(_single("dirty"))
    store.write_attempt("dirty", _attempt())

    state = load_state(_entry(tmp_path))

    stages = {stage.name: stage for stage in state.stages}
    assert stages["attempt-only"].status == "interrupted"
    assert stages["attempt-only"].artifacts == []
    assert stages["dirty"].status == "interrupted"
    assert stages["dirty"].artifacts[0].path == Path("dirty.txt")
    assert state.overall == "interrupted"


def test_load_state_isolates_corrupt_stage_files(tmp_path: Path) -> None:
    store = Store(tmp_path)
    (tmp_path / "healthy.txt").write_text("ok", encoding="utf-8")
    store.write_success(_single("healthy"))
    corrupt_path = tmp_path / ".varve" / "stages" / "broken.json"
    corrupt_path.parent.mkdir(parents=True, exist_ok=True)
    corrupt_path.write_text("{bad", encoding="utf-8")

    state = load_state(_entry(tmp_path))

    stages = {stage.name: stage for stage in state.stages}
    assert stages["healthy"].status == "ok"
    assert stages["broken"].status == "corrupt"
    assert stages["broken"].artifacts == []
    assert state.overall == "corrupt"


def test_load_state_forces_corrupt_overall_when_manifest_name_is_missing(
    tmp_path: Path,
) -> None:
    (tmp_path / "ok.txt").write_text("ok", encoding="utf-8")
    Store(tmp_path).write_success(_single("ok"))

    state = load_state(_entry(tmp_path, experiment_name=None))

    assert state.overall == "corrupt"


def test_load_state_marks_manifest_only_experiment_empty(tmp_path: Path) -> None:
    Store(tmp_path).ensure_initialized("Demo")

    state = load_state(_entry(tmp_path))

    assert state.stages == []
    assert state.order == []
    assert state.overall == "empty"


def test_load_state_rebuilds_dag_order_from_upstream_keys(tmp_path: Path) -> None:
    store = Store(tmp_path)
    for name in ["a.txt", "b.txt", "c.txt", "d.txt"]:
        (tmp_path / name).write_text(name, encoding="utf-8")
    store.write_success(_single("a"))
    store.write_success(_single("b", upstreams={"a": {"content_key": "sha256:a"}}))
    store.write_success(_single("c", upstreams={"a": {"content_key": "sha256:a"}}))
    store.write_success(_single("d"))

    state = load_state(_entry(tmp_path))

    assert set(state.order) == {"a", "b", "c", "d"}
    assert state.order.index("a") < state.order.index("b")
    assert state.order.index("a") < state.order.index("c")
    stages = {stage.name: stage for stage in state.stages}
    assert stages["b"].upstreams == ["a"]
    assert stages["c"].upstreams == ["a"]
    assert stages["d"].upstreams == []


def test_load_state_ignores_non_iso_committed_at(tmp_path: Path) -> None:
    (tmp_path / "sample.txt").write_text("ok", encoding="utf-8")
    Store(tmp_path).write_success(_single("sample", committed_at="now"))

    state = load_state(_entry(tmp_path))

    assert state.stages[0].committed_at is None
