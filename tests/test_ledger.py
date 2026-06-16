from __future__ import annotations

from pathlib import Path

import pytest

from varve.ledger import CorruptLedger, Ledger
from varve.models import (
    AttemptMarker,
    BatchRecord,
    KeyComponents,
    OutputHandle,
    PartialMeta,
    SuccessRecord,
)


def _components() -> KeyComponents:
    return KeyComponents(source={}, config={}, files={}, values={}, upstreams={})


def test_ledger_initializes_gitignore_and_manifest(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    ledger.ensure_initialized("Demo")
    assert (tmp_path / ".varve" / ".gitignore").read_text(encoding="utf-8") == "*\n"
    with pytest.raises(ValueError, match="belongs to Demo"):
        ledger.ensure_initialized("Other")


def test_success_round_trip_and_tmp_does_not_pollute(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    ledger.ensure_initialized("Demo")
    record = SuccessRecord(
        experiment="Demo",
        stage="transform",
        kind="batch",
        content_key="sha256:a",
        key_components=_components(),
        outputs=[OutputHandle(index=0, path="part-0.txt")],
        committed_at="now",
    )
    ledger.write_success(record)
    (tmp_path / ".varve" / "stages" / "transform.json.tmp").write_text("{bad", encoding="utf-8")
    assert ledger.read_success("transform") == record


def test_attempt_and_partial_round_trip(tmp_path: Path) -> None:
    ledger = Ledger(tmp_path)
    ledger.ensure_initialized("Demo")
    marker = AttemptMarker(content_key="sha256:a", started_at="now", touched_existing=False)
    ledger.write_attempt("sample", marker)
    assert ledger.read_attempt("sample") == marker
    ledger.clear_attempt("sample")
    assert ledger.read_attempt("sample") is None

    meta = PartialMeta(content_key="sha256:a", partition_values={"batch": 1}, started_at="now")
    ledger.write_partial_meta("batch", "run", meta)
    ledger.write_batch("batch", "run", BatchRecord(index=1, yielded=["b"], committed_at="now"))
    ledger.write_batch("batch", "run", BatchRecord(index=0, yielded=["a"], committed_at="now"))
    read = ledger.read_partial("batch", "run")
    assert read is not None
    assert read[0] == meta
    assert sorted(read[1]) == [0, 1]


def test_corrupt_ledger_raises(tmp_path: Path) -> None:
    path = tmp_path / ".varve" / "stages"
    path.mkdir(parents=True)
    (path / "sample.json").write_text("{bad", encoding="utf-8")
    with pytest.raises(CorruptLedger):
        Ledger(tmp_path).read_success("sample")

