from __future__ import annotations

from varve.log import BatchProgress, get_logger


def test_get_logger_name() -> None:
    assert get_logger().name == "varve"


def test_batch_progress_throttles_by_count() -> None:
    progress = BatchProgress(total=5, every=2, min_interval=999)
    assert progress.tick(0) is None
    assert progress.tick(1) is not None
    assert progress.tick(2) is None
    assert progress.tick(4) is not None

