from __future__ import annotations

import pickle
from enum import Enum
from pathlib import Path

import pytest

from varve import Axis
from varve.axis import Cell, canonical_id


class Color(Enum):
    RED = "red"
    BLUE = "blue"


class Level(Enum):
    LOW = 1
    HIGH = 2


class Shape(Enum):
    BOX = (1, 2)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("mw-2024", "mw-2024"),
        (7, "7"),
        (Color.RED, "red"),
        (Level.HIGH, "2"),
        (Shape.BOX, "BOX"),
        ("a b", "a b"),
    ],
)
def test_canonical_id(value: object, expected: str) -> None:
    assert canonical_id(value) == expected


@pytest.mark.parametrize("value", ["", ".", "..", "a/b", "a,b", "a=b", "a\0b"])
def test_canonical_id_rejects_unsafe_ids(value: str) -> None:
    with pytest.raises(ValueError, match="Invalid axis id"):
        canonical_id(value)


@pytest.mark.parametrize("value", [True, 1.5, None, Path("x")])
def test_canonical_id_rejects_other_types(value: object) -> None:
    with pytest.raises(TypeError):
        canonical_id(value)


def test_static_axis_keeps_declaration_order_and_values() -> None:
    axis = Axis("color", (Color.BLUE, Color.RED))
    assert axis.is_static
    assert axis.ids == ("blue", "red")
    domain = axis.static_domain()
    assert domain.values == (Color.BLUE, Color.RED)
    assert domain.keys == (Color.BLUE, Color.RED)
    assert domain.position("red") == 1


def test_static_axis_validation() -> None:
    with pytest.raises(ValueError, match="duplicate id"):
        Axis("n", (1, "1"))
    with pytest.raises(ValueError, match="at least one"):
        Axis("n", ())
    with pytest.raises(TypeError, match="sequence"):
        Axis("n", "abc")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exactly one"):
        Axis("n")
    with pytest.raises(ValueError, match="exactly one"):
        Axis("n", ("a",), resolver=lambda ctx: ["a"])
    with pytest.raises(ValueError, match="dynamic axes only"):
        Axis("n", ("a",), input_fingerprint="stat")
    for bad in ("1x", "a-b", "class", "ctx", "self"):
        with pytest.raises(ValueError):
            Axis(bad, ("a",))


def test_dynamic_axis_domain_shapes(tmp_path: Path) -> None:
    axis = Axis("tar", resolver=lambda ctx: {}, input_fingerprint="stat")
    assert not axis.is_static
    with pytest.raises(TypeError, match="dynamic"):
        _ = axis.ids

    paths = axis.domain_from_resolved({"b": tmp_path / "b", "a": tmp_path / "a"})
    assert paths.ids == ("b", "a")  # resolver order is kept
    assert paths.is_path and paths.values == (tmp_path / "b", tmp_path / "a")

    mapped = axis.domain_from_resolved({1: {"n": 1}, 2: {"n": 2}})
    assert mapped.ids == ("1", "2") and mapped.keys == (1, 2) and not mapped.is_path

    keys = axis.domain_from_resolved([3, 1, 2])
    assert keys.ids == ("3", "1", "2") and keys.values == (3, 1, 2)


def test_dynamic_axis_rejects_bad_results(tmp_path: Path) -> None:
    axis = Axis("batch", resolver=lambda ctx: [])
    with pytest.raises(TypeError, match="mixed Path"):
        axis.domain_from_resolved({"a": tmp_path, "b": 1})
    with pytest.raises(TypeError, match="Mapping or a Sequence"):
        axis.domain_from_resolved("abc")
    with pytest.raises(TypeError, match="str or int"):
        axis.domain_from_resolved([Color.RED])
    with pytest.raises(ValueError, match="duplicate"):
        axis.domain_from_resolved([1, "1"])


def test_domain_restrict_keeps_domain_order() -> None:
    domain = Axis("d", ("a", "b", "c")).static_domain()
    assert domain.restrict(["c", "a"]).ids == ("a", "c")
    with pytest.raises(ValueError, match="Unknown id 'z'"):
        domain.restrict(["z"])


def test_cell_mapping_attribute_access_and_pickle() -> None:
    cell = Cell({"dataset": "mw", "cost_group": "texform"})
    assert cell.cost_group == "texform"
    assert dict(cell) == {"dataset": "mw", "cost_group": "texform"}
    with pytest.raises(AttributeError):
        _ = cell.missing
    assert pickle.loads(pickle.dumps(cell)) == cell
