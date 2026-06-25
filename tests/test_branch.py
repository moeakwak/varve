from __future__ import annotations

import pytest

from varve.branch import validate_branch_name


@pytest.mark.parametrize("name", ["main", "a.b-c_1", "A0"])
def test_validate_branch_name_accepts_single_safe_segment(name: str) -> None:
    assert validate_branch_name(name) == name


@pytest.mark.parametrize("name", ["", "a/b", "..", "/abs", ".tmp"])
def test_validate_branch_name_rejects_unsafe_names(name: str) -> None:
    with pytest.raises(ValueError, match="branch name"):
        validate_branch_name(name)
