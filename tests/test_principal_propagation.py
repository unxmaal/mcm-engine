"""The transport principal must reach tool handlers through the middleware
boundary (audit M2 for issue #83).

`BearerTokenMiddleware` binds the bearer-token principal in `dispatch()` (before
`call_next`) and tool handlers read it via `resolve_actor()`. Starlette's
`BaseHTTPMiddleware` historically ran the endpoint in a child anyio task — but a
contextvar set BEFORE `call_next` still propagates DOWN to the endpoint (only the
reverse, endpoint->middleware, is lost). This test pins that behavior with the
REAL `principal` module, so a future Starlette bump that breaks propagation is
caught here instead of silently misattributing every authenticated write to
`nobody`.
"""
from __future__ import annotations

import anyio
import httpx
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from mcm_engine.principal import reset_principal, resolve_actor, set_principal


class _PrincipalMiddleware(BaseHTTPMiddleware):
    """Mirrors transport.BearerTokenMiddleware's principal binding."""

    async def dispatch(self, request, call_next):
        token = set_principal("alice@example.com")
        try:
            return await call_next(request)
        finally:
            reset_principal(token)


async def _endpoint(request):
    # Exactly what a rule tool's resolve_actor("") observes mid-request.
    return PlainTextResponse(resolve_actor(""))


def _get(app) -> str:
    async def go() -> str:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            return (await c.get("/")).text

    return anyio.run(go)


def test_transport_principal_reaches_handler_through_middleware(monkeypatch):
    monkeypatch.delenv("MCM_ACTOR", raising=False)
    app = Starlette(
        routes=[Route("/", _endpoint)],
        middleware=[Middleware(_PrincipalMiddleware)],
    )
    assert _get(app) == "alice@example.com"


def test_resolve_actor_falls_back_without_a_principal(monkeypatch):
    monkeypatch.delenv("MCM_ACTOR", raising=False)
    # No middleware, no principal bound: the terminal fallback, and explicit wins.
    assert resolve_actor("") == "nobody"
    assert resolve_actor("explicit-actor") == "explicit-actor"
