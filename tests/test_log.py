from __future__ import annotations

from varve.log import get_logger


def test_get_logger_name() -> None:
    assert get_logger().name == "varve"
