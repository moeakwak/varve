from __future__ import annotations

from varve.astkey import _normalized_source_hash, source_hash


def same_logic(x: int) -> int:
    return x + 1


def same_logic_with_different_name(x: int) -> int:
    return x + 1


def different_logic(x: int) -> int:
    return x + 2


def test_source_hash_ignores_docstrings_and_comments() -> None:
    assert _normalized_source_hash(
        """
        def fn(x):
            '''alpha'''
            # a comment
            return x + 1
        """
    ) == _normalized_source_hash(
        """
        def fn(x):
            '''bravo'''
            return x + 1
        """
    )


def test_source_hash_keeps_definition_names() -> None:
    assert source_hash(same_logic) != source_hash(same_logic_with_different_name)


def test_source_hash_changes_when_logic_changes() -> None:
    assert source_hash(same_logic) != source_hash(different_logic)


class SourceHashExample:
    """Class docstrings are stripped too."""

    value = 1


class SourceHashExampleWithDifferentName:
    """Another docstring."""

    value = 1


def test_source_hash_keeps_class_names() -> None:
    assert source_hash(SourceHashExample) != source_hash(SourceHashExampleWithDifferentName)
