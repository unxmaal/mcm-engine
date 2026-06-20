"""HTTP/SSE transport for mcm-engine (MCM2-20).

The default ``mcm-engine run`` uses stdio for Claude Code's
spawn-engine flow. The daemon deployment serves over HTTP/SSE, which
lets a single long-lived engine answer many short-lived agents and
unlocks the watcher cascade (MCM2-23): file changes that happen while
no tool call is in flight still get picked up because the daemon is
already running.

This module wires FastMCP's SSE app behind a Starlette router that
adds operational endpoints:
  - GET /healthz — liveness probe; never depends on adapter health
  - GET /readyz  — readiness probe; pings every wired adapter
"""
from __future__ import annotations

import contextlib
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route


def _liveness(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def _make_readyz(server: Any):
    """Build the /readyz handler against a wired MCMServer.

    Liveness is cheap; readiness probes every adapter so an operator
    can tell whether storage / counters / search are responsive without
    SSH-ing into the box.
    """
    def readyz(_request: Request) -> JSONResponse:
        checks: dict[str, str] = {}
        overall_ok = True

        # Storage — a count is the cheapest read every backend
        # supports.
        try:
            server.ctx.storage.count_relations()
            checks["storage"] = "ok"
        except Exception as e:
            checks["storage"] = f"error: {type(e).__name__}"
            overall_ok = False

        # Counters — ``flush`` returns None; any exception means the
        # counter store is unreachable.
        try:
            server.ctx.counters.flush()
            checks["counters"] = "ok"
        except Exception as e:
            checks["counters"] = f"error: {type(e).__name__}"
            overall_ok = False

        # Search — an empty-query search either returns [] or raises;
        # we accept either as "responsive."
        try:
            server.ctx.search.search("", limit=1)
            checks["search"] = "ok"
        except Exception as e:
            checks["search"] = f"error: {type(e).__name__}"
            overall_ok = False

        status = 200 if overall_ok else 503
        return JSONResponse(
            {"status": "ok" if overall_ok else "degraded", "checks": checks},
            status_code=status,
        )

    return readyz


def build_asgi_app(server: Any, *, transport: str = "sse") -> Starlette:
    """Build the public ASGI app: FastMCP transport + /healthz + /readyz.

    ``transport`` selects the FastMCP transport sub-app. Valid values:
      - "sse"             — Server-Sent Events under /sse
      - "streamable-http" — newer MCP HTTP transport under /mcp

    Either way the SSE/HTTP app is mounted at the root and our
    operational routes live alongside it.
    """
    if transport == "sse":
        mcp_app = server.mcp.sse_app()
    elif transport == "streamable-http":
        mcp_app = server.mcp.streamable_http_app()
    else:
        raise ValueError(
            f"unknown transport {transport!r}; expected 'sse' or 'streamable-http'"
        )

    routes = [
        Route("/healthz", _liveness),
        Route("/readyz", _make_readyz(server)),
        Mount("/", app=mcp_app),
    ]

    inner_lifespan = getattr(mcp_app, "lifespan", None)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        # Daemon-mode startup: bring the DB current with disk and start
        # the file watcher (MCM2-23). Wrapped to be no-op-safe when the
        # server has no .watcher attribute (e.g., test doubles).
        if hasattr(server, "start_watcher"):
            try:
                server.start_watcher()
            except Exception:
                pass
        try:
            if inner_lifespan is not None:
                async with inner_lifespan(app):
                    yield
            else:
                yield
        finally:
            if hasattr(server, "stop_watcher"):
                try:
                    server.stop_watcher()
                except Exception:
                    pass

    return Starlette(routes=routes, lifespan=lifespan)


def serve(
    server: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    transport: str = "sse",
) -> None:
    """Run the engine over HTTP/SSE. Blocks until the process is killed."""
    import uvicorn

    app = build_asgi_app(server, transport=transport)
    uvicorn.run(app, host=host, port=port, log_level="info")
