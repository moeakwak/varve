from __future__ import annotations

from pathlib import Path

import pytest

from varve.models import (
    AttemptMarker,
    BatchRecord,
    KeyComponents,
    OutputHandle,
    PartialMeta,
    SuccessRecord,
)
from varve.store.store import CorruptStore, Store


def _components() -> KeyComponents:
    return KeyComponents(source={}, config={}, files={}, values={}, upstreams={})


def test_store_initializes_gitignore_and_manifest(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.ensure_initialized("Demo", module="pkg.demo", temporary_config={"token": "x"})
    assert (tmp_path / ".varve" / ".gitignore").read_text(encoding="utf-8") == "*\n"
    manifest = store.read_manifest()
    assert manifest is not None
    assert manifest.module == "pkg.demo"
    assert manifest.temporary_config == {"token": "x"}
    with pytest.raises(ValueError, match="belongs to Demo"):
        store.ensure_initialized("Other")


def test_store_updates_manifest_module(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.ensure_initialized("Demo", module="pkg.old")
    store.ensure_initialized("Demo", module="pkg.new")

    manifest = store.read_manifest()

    assert manifest is not None
    assert manifest.module == "pkg.new"


def test_success_round_trip_and_tmp_does_not_pollute(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.ensure_initialized("Demo")
    record = SuccessRecord(
        experiment="Demo",
        stage="transform",
        kind="batch",
        content_key="sha256:a",
        key_components=_components(),
        outputs=[OutputHandle(index=0, path="part-0.txt")],
        committed_at="now",
    )
    store.write_success(record)
    (tmp_path / ".varve" / "stages" / "transform.json.tmp").write_text("{bad", encoding="utf-8")
    assert store.read_success("transform") == record


def test_attempt_and_partial_round_trip(tmp_path: Path) -> None:
    store = Store(tmp_path)
    store.ensure_initialized("Demo")
    marker = AttemptMarker(content_key="sha256:a", started_at="now", touched_existing=False)
    store.write_attempt("sample", marker)
    assert store.read_attempt("sample") == marker
    store.clear_attempt("sample")
    assert store.read_attempt("sample") is None

    meta = PartialMeta(content_key="sha256:a", partition_values={"batch": 1}, started_at="now")
    store.write_partial_meta("batch", "run", meta)
    store.write_batch("batch", "run", BatchRecord(index=1, yielded=["b"], committed_at="now"))
    store.write_batch("batch", "run", BatchRecord(index=0, yielded=["a"], committed_at="now"))
    read = store.read_partial("batch", "run")
    assert read is not None
    assert read[0] == meta
    assert sorted(read[1]) == [0, 1]


def test_corrupt_store_raises(tmp_path: Path) -> None:
    path = tmp_path / ".varve" / "stages"
    path.mkdir(parents=True)
    (path / "sample.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(CorruptStore):
        Store(tmp_path).read_success("sample")
