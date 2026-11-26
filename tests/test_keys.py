from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from varve import Axis
from varve.graph import Unit
from varve.keying.fingerprint import FingerprintSession
from varve.keying.keys import (
    SEGMENTS,
    changed_segments,
    compute_input_key,
    source_match,
)


class Config(BaseModel):
    seed: int = 1
    model: str = "small"


def key(**overrides: Any):
    arguments: dict[str, Any] = {
        "config": Config(),
        "config_access": ["seed"],
        "upstream": {"prepare": "fp-1"},
        "inputs": {},
        "values": {"engine": "1.0"},
        "version": None,
    }
    arguments.update(overrides)
    return compute_input_key(**arguments)


def test_components_have_five_segments_with_hashes_and_summaries() -> None:
    result = key()
    assert list(result.components) == list(SEGMENTS)
    assert result.components["config"]["summary"] == {"seed": 1}
    assert result.components["config"]["access"] == ["seed"]
    assert result.components["version"]["summary"] is None
    assert all(len(item["hash"]) == 64 for item in result.components.values())
    assert len(result.input_key) == 64 and result.file_sets == []


@pytest.mark.parametrize(
    ("overrides", "segment"),
    [
        ({"config": Config(seed=2)}, "config"),
        ({"upstream": {"prepare": "fp-2"}}, "upstream"),
        ({"values": {"engine": "2.0"}}, "values"),
        ({"version": "2"}, "version"),
    ],
)
def test_each_segment_changes_the_key(overrides: dict[str, Any], segment: str) -> None:
    base, changed = key(), key(**overrides)
    assert base.input_key != changed.input_key
    assert changed_segments(base.components, changed.components) == [segment]


def test_config_projection_ignores_unread_fields() -> None:
    assert key(config=Config(model="large")).input_key == key().input_key
    assert key(config_access=None).components["config"]["summary"] == {"seed": 1, "model": "small"}
    assert key(config={"seed": 1, "other": 3}).input_key == key().input_key


def test_small_inputs_inline_large_inputs_go_to_file_sets(tmp_path: Path) -> None:
    small = tmp_path / "small"
    big = tmp_path / "big"
    for index in range(3):
        (small / f"{index}.txt").parent.mkdir(exist_ok=True)
        (small / f"{index}.txt").write_text(str(index))
    for index in range(40):
        (big / f"{index:02}.txt").parent.mkdir(exist_ok=True)
        (big / f"{index:02}.txt").write_text(str(index))
    session = FingerprintSession()
    inputs = session.inputs({"small": small, "big": big})
    first = key(inputs=inputs)
    summary = first.components["inputs"]["summary"]
    assert "entries" in summary["small"] and "fileset" not in summary["small"]
    assert summary["big"]["fileset"] == inputs["big"].digest and "entries" not in summary["big"]
    (row,) = first.file_sets
    assert row.fileset_hash == inputs["big"].digest and row.file_count == 40
    # Two units sharing the big input reference one content-addressed file set.
    second = key(inputs=inputs, values={"engine": "other"})
    assert second.file_sets[0].fileset_hash == row.fileset_hash
    (small / "0.txt").write_text("changed")
    third = key(inputs=FingerprintSession().inputs({"small": small, "big": big}))
    assert changed_segments(first.components, third.components) == ["inputs"]


def test_source_match_rule() -> None:
    assert source_match(version=None, current="s1", recorded="s1", reused=None) == "match"
    assert source_match(version=None, current="s2", recorded="s1", reused=None) == "changed"
    assert source_match(version=None, current="s2", recorded="s1", reused="s2") == "reused"
    assert source_match(version="2", current="s2", recorded="s1", reused=None) == "code-modified"


def test_dynamic_axis_paths_are_inputs_and_other_values_are_values(tmp_path: Path) -> None:
    from varve.dependencies import PathSet

    batch = Axis("batch", resolver=lambda ctx: {})
    shard = Axis("shard", resolver=lambda ctx: [])
    unit = Unit("s", (batch, shard), ("1", "2"), (1, 2), (tmp_path / "b1", 2), "batch=1,shard=2")
    assert unit.axis_inputs() == {"batch": PathSet((tmp_path / "b1",))}
    assert unit.axis_values() == {"shard": 2}


def test_fast_hash_helpers_match_canonical_json_hash() -> None:
    from varve.keying.fingerprint import json_hash
    from varve.keying.keys import combine_hashes, plain_hash

    hashes = {
        name: json_hash(name) for name in ("config", "inputs", "upstream", "values", "version")
    }
    assert combine_hashes(hashes) == json_hash(hashes)
    plain = {"b": [["k=1", "fp"]], "a": "x", "é": 1.5, "n": None}
    assert plain_hash(plain) == json_hash(plain)
