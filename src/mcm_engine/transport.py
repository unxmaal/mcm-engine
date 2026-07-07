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

LODESTONE additive surface:
  - POST /v1/claims — REST shim the sieve POSTs to after the regex
    pass clears. Wraps storage.insert_knowledge with the Claim-shaped
    fields (subject_keys, governance_tags, scope, status, provenance).
  - Bearer-token middleware applied to every route except /healthz
    and /readyz when MCM_AUTH_REQUIRED=true.
"""
from __future__ import annotations

import contextlib
import json
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from . import tokens as _tokens
from .principal import reset_principal as _reset_principal
from .principal import set_principal as _set_principal


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


# ---------------------------------------------------------------------------
# LODESTONE additive surface: bearer-token middleware + /v1/claims shim.
# ---------------------------------------------------------------------------

# Paths the middleware lets through without auth. Health probes must
# stay reachable to Kubernetes liveness/readiness checks regardless
# of token configuration.
_UNAUTHENTICATED_PATHS: frozenset[str] = frozenset({"/healthz", "/readyz"})


class BearerTokenMiddleware(BaseHTTPMiddleware):
    """Validate Authorization: Bearer <token> against the tokens table.

    Engaged only when MCM_AUTH_REQUIRED=true (see tokens.auth_required).
    Sets ``request.state.principal`` on success so downstream routes
    (e.g. /v1/claims, future kb_recall) can attribute writes.
    """

    def __init__(self, app, *, server: Any):
        super().__init__(app)
        self._server = server

    async def dispatch(self, request: Request, call_next):
        if not _tokens.auth_required():
            return await call_next(request)
        if request.url.path in _UNAUTHENTICATED_PATHS:
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return JSONResponse(
                {"error": "bearer token required"}, status_code=401
            )
        plaintext = header.split(" ", 1)[1].strip()
        try:
            # Borrow a connection from the storage pool for token validation, so
            # this out-of-band SELECT/UPDATE/commit runs on its OWN connection and
            # never lands inside a tool handler's transaction (issue #83 / H3).
            storage = self._server.ctx.storage
            with storage._pool.connection() as conn:
                principal = _tokens.validate_token(conn, plaintext)
        except Exception as e:
            return JSONResponse(
                {"error": f"token validation error: {type(e).__name__}"},
                status_code=500,
            )
        if principal is None:
            return JSONResponse(
                {"error": "invalid or revoked token"}, status_code=401
            )
        request.state.principal = principal
        # Bind for rule-provenance actor resolution (issue #10) so tool
        # handlers can attribute writes without the request object.
        token = _set_principal(principal)
        try:
            return await call_next(request)
        finally:
            _reset_principal(token)


def _make_claims_endpoint(server: Any):
    """POST /v1/claims — sieve forwards accepted pushes here.

    Body schema (additive on top of mcm-engine's KnowledgeRow):
        {
          "claim":            "...",            # required
          "subject_keys":     ["..."],          # optional
          "governance_tags":  ["..."],          # optional
          "scope":            "...",            # optional
          "status":           "active",         # optional
          "provenance":       [{...}, ...],     # optional
          "topic":            "...",            # optional, defaults to "" (auto-derived)
          "kind":             "finding",        # optional, defaults to "finding"
          "project":          "...",            # optional
          "tags":             "csv,here",       # optional, mcm-engine native
        }

    Returns 201 with {"id": <new_id>} on accept, 400 on schema error,
    401 if bearer auth is on and missing/invalid.
    """
    async def claims(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

        claim = body.get("claim")
        if not isinstance(claim, str) or not claim.strip():
            return JSONResponse(
                {"error": "claim is required and must be a non-empty string"},
                status_code=400,
            )

        # Map Claim fields onto mcm-engine's existing knowledge row.
        # The sieve has already accepted; this layer's job is the
        # native insert plus the additive Claim columns.
        topic = body.get("topic") or claim[:80]
        kind = body.get("kind") or "finding"
        summary = claim
        detail = body.get("detail") or None
        tags = body.get("tags") or None
        project = body.get("project") or None

        subject_keys = body.get("subject_keys") or []
        governance_tags = body.get("governance_tags") or []
        scope = body.get("scope") or None
        status = body.get("status") or "active"
        provenance = body.get("provenance") or []

        if not isinstance(subject_keys, list) or not all(isinstance(x, str) for x in subject_keys):
            return JSONResponse({"error": "subject_keys must be a list of strings"}, status_code=400)
        if not isinstance(governance_tags, list) or not all(isinstance(x, str) for x in governance_tags):
            return JSONResponse({"error": "governance_tags must be a list of strings"}, status_code=400)
        if not isinstance(provenance, list):
            return JSONResponse({"error": "provenance must be a list"}, status_code=400)

        storage = server.ctx.storage
        principal = getattr(request.state, "principal", None) or "anonymous"

        try:
            # Own connection from the pool (issue #83 / audit H3): this insert
            # never shares a connection with a tool handler's transaction. The
            # pool's context commits on clean exit / rolls back on error.
            with storage._pool.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO knowledge
                            (topic, kind, summary, detail, tags, project,
                             subject_keys, governance_tags, scope, status, provenance)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        RETURNING id
                        """,
                        (
                            topic, kind, summary, detail, tags, project,
                            subject_keys, governance_tags, scope, status,
                            json.dumps(provenance),
                        ),
                    )
                    row = cur.fetchone()
        except Exception as e:
            return JSONResponse(
                {"error": f"insert failed: {type(e).__name__}: {e}"},
                status_code=500,
            )

        new_id = row["id"] if hasattr(row, "keys") else row[0]
        return JSONResponse(
            {"id": new_id, "principal": principal},
            status_code=201,
        )

    return claims


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
        Route("/v1/claims", _make_claims_endpoint(server), methods=["POST"]),
        Mount("/", app=mcp_app),
    ]

    middleware = [
        Middleware(BearerTokenMiddleware, server=server),
    ]

    inner_lifespan = getattr(mcp_app, "lifespan", None) or getattr(
        getattr(mcp_app, "router", None), "lifespan_context", None
    )

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

    return Starlette(routes=routes, middleware=middleware, lifespan=lifespan)


_LOCALHOST_HOST_PATTERNS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_LOCALHOST_ORIGIN_PATTERNS = [
    "http://127.0.0.1:*",
    "http://localhost:*",
    "http://[::1]:*",
]
_WILDCARD_BINDS = frozenset({"0.0.0.0", "::", ""})


def _host_pattern(value: str) -> str | None:
    """Normalize an allowed-host entry to a form the MCP transport-security
    matcher understands. A bare host becomes ``host:*`` (port wildcard); an
    entry that already carries an explicit port or ``:*`` is left alone."""
    h = value.strip()
    if not h:
        return None
    # Bracketed IPv6, e.g. "[::1]" or "[::1]:8080".
    if h.startswith("["):
        return h if "]:" in h else f"{h}:*"
    if ":" in h:
        tail = h.rsplit(":", 1)[1]
        if tail == "*" or tail.isdigit():
            return h
    return f"{h}:*"


def _local_ipv4s() -> list[str]:
    """Best-effort enumeration of this host's LAN IPv4 addresses. Used when
    binding to a wildcard address so the reachable interface IPs are allowed
    without the operator having to name them (survives DHCP churn)."""
    import socket

    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1: no packets sent, just routes
            ips.add(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        pass
    return sorted(i for i in ips if not i.startswith("127."))


def _configure_transport_security(
    server: Any,
    *,
    host: str,
    allowed_hosts: list[str] | None = None,
    enable: bool = True,
) -> None:
    """Align FastMCP's DNS-rebinding allow-list with the address uvicorn
    actually binds to.

    FastMCP auto-enables DNS-rebinding protection with a localhost-only
    allow-list whenever its internal ``host`` is localhost — which it always
    is here, because ``MCMServer`` builds ``FastMCP`` without a host while
    uvicorn binds wherever ``serve`` is told. The result is that every LAN
    request (Host: <lan-ip>:<port>) is rejected with ``421 Invalid Host
    header``. This re-derives the allow-list from the real bind host.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    mcp = getattr(server, "mcp", None)
    settings = getattr(mcp, "settings", None)
    if settings is None:
        return

    if not enable:
        settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
        return

    hosts = list(_LOCALHOST_HOST_PATTERNS)
    origins = list(_LOCALHOST_ORIGIN_PATTERNS)

    extra: list[str] = list(allowed_hosts or [])
    if host in _WILDCARD_BINDS:
        # Bound to every interface: allow this machine's LAN IPs so it is
        # reachable by address, not just by loopback.
        extra.extend(_local_ipv4s())
    elif host not in ("127.0.0.1", "localhost", "::1"):
        # Bound to one concrete non-loopback address: allow exactly that.
        extra.append(host)

    for entry in extra:
        pat = _host_pattern(entry)
        if pat and pat not in hosts:
            hosts.append(pat)
            origins.append(f"http://{pat}")

    settings.transport_security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def serve(
    server: Any,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    transport: str = "sse",
    allowed_hosts: list[str] | None = None,
    dns_rebinding_protection: bool = True,
) -> None:
    """Run the engine over HTTP/SSE. Blocks until the process is killed."""
    import uvicorn

    _configure_transport_security(
        server,
        host=host,
        allowed_hosts=allowed_hosts,
        enable=dns_rebinding_protection,
    )
    app = build_asgi_app(server, transport=transport)
    uvicorn.run(app, host=host, port=port, log_level="info")
