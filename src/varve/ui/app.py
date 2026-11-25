"""Starlette application: six read-only JSON endpoints plus the static frontend."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

try:
    import uvicorn
    from starlette.applications import Starlette
    from starlette.exceptions import HTTPException
    from starlette.requests import Request
    from starlette.responses import FileResponse, JSONResponse
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles
except ImportError as exc:  # pragma: no cover - depends on the environment
    raise ImportError(
        "the varve web UI needs optional dependencies; install them with "
        "`pip install 'varve[ui]'` (or `uv add 'varve[ui]'`)"
    ) from exc

from varve.store.db import SchemaError
from varve.ui import queries
from varve.ui.queries import DbSource, NotFoundError, Registry

STATIC_DIR = Path(__file__).with_name("static")


def _int(request: Request, name: str, default: int) -> int:
    raw = request.query_params.get(name)
    if raw in (None, ""):
        return default
    try:
        return int(raw)
    except ValueError:
        raise HTTPException(400, f"query parameter {name!r} must be an integer") from None


def _opt(request: Request, name: str) -> str | None:
    value = request.query_params.get(name)
    return value if value not in (None, "") else None


def create_app(source: DbSource) -> Starlette:
    """Build the app over a fixed list of ``state.db`` paths or a discovery callable.

    A callable is re-evaluated on every ``/api/workspace`` request (and when
    an unknown pipeline id is requested), so new pipelines appear on reload.
    """

    registry = Registry(source)

    def workspace(request: Request) -> JSONResponse:
        return JSONResponse(queries.workspace(registry))

    def branch(request: Request) -> JSONResponse:
        pipe = registry.get(request.path_params["pipeline"])
        return JSONResponse(queries.branch_detail(pipe, request.path_params["branch"]))

    def units(request: Request) -> JSONResponse:
        pipe = registry.get(request.path_params["pipeline"])
        params = request.query_params
        return JSONResponse(
            queries.stage_units(
                pipe,
                request.path_params["branch"],
                request.path_params["stage"],
                status=_opt(request, "status"),
                error_type=params.get("error_type"),
                error_summary=params.get("error_summary"),
                q=_opt(request, "q"),
                sort=params.get("sort", "key"),
                page=_int(request, "page", 1),
                page_size=_int(request, "page_size", 50),
            )
        )

    def unit(request: Request) -> JSONResponse:
        pipe = registry.get(request.path_params["pipeline"])
        p = request.path_params
        return JSONResponse(queries.unit_detail(pipe, p["branch"], p["stage"], p["unit_key"]))

    def history(request: Request) -> JSONResponse:
        pipe = registry.get(request.path_params["pipeline"])
        return JSONResponse(
            queries.history(
                pipe,
                request.path_params["branch"],
                stage=_opt(request, "stage"),
                limit=_int(request, "limit", 50),
            )
        )

    def run(request: Request) -> JSONResponse:
        return JSONResponse(
            queries.run_detail(registry, request.path_params["run_id"], _opt(request, "pipeline"))
        )

    def index(request: Request) -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    async def not_found(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=404)

    async def http_error(request: Request, exc: Exception) -> JSONResponse:
        assert isinstance(exc, HTTPException)
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    async def db_error(request: Request, exc: Exception) -> JSONResponse:
        locked = isinstance(exc, sqlite3.OperationalError) and "locked" in str(exc)
        return JSONResponse(
            {"error": f"{type(exc).__name__}: {exc}"}, status_code=503 if locked else 500
        )

    async def schema_error(request: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=409)

    prefix = "/api/pipelines/{pipeline}/branches/{branch}"
    routes: list[Any] = [
        Route("/", index),
        Route("/api/workspace", workspace),
        Route(prefix, branch),
        Route(f"{prefix}/history", history),
        Route(f"{prefix}/stages/{{stage}}/units", units),
        Route(f"{prefix}/stages/{{stage}}/units/{{unit_key}}", unit),
        Route("/api/runs/{run_id}", run),
        Mount("/static", StaticFiles(directory=STATIC_DIR), name="static"),
    ]
    return Starlette(
        routes=routes,
        exception_handlers={
            NotFoundError: not_found,
            FileNotFoundError: not_found,
            HTTPException: http_error,
            SchemaError: schema_error,
            sqlite3.Error: db_error,
        },
    )


def serve(
    source: DbSource,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
    log_level: str = "warning",
) -> None:
    """Run the UI in the foreground until interrupted (Ctrl-C)."""

    app = create_app(source)
    print(f"varve ui: serving on http://{host}:{port}/  (Ctrl-C to stop)", flush=True)
    uvicorn.run(app, host=host, port=port, log_level=log_level)
