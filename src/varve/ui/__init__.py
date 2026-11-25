"""Local read-only web UI over ``.varve/state.db`` files (optional extra ``varve[ui]``).

``discover_state_dbs`` has no extra dependencies. ``create_app`` and
``serve`` load Starlette/Uvicorn on first access and raise an
``ImportError`` that names ``varve[ui]`` when they are missing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from varve.ui.discovery import discover_state_dbs

if TYPE_CHECKING:
    from varve.ui.app import create_app, serve

__all__ = ["create_app", "discover_state_dbs", "serve"]


def __getattr__(name: str) -> Any:
    if name in ("create_app", "serve"):
        from varve.ui import app

        return getattr(app, name)
    raise AttributeError(f"module 'varve.ui' has no attribute {name!r}")
