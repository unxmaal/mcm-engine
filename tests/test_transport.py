"""MCM2-20: HTTP/SSE transport — /healthz and /readyz probes.

Verifies the build_asgi_app wrapper:
  - /healthz is liveness; always 200 regardless of adapter state.
  - /readyz pings each adapter and reports per-adapter status.
  - The FastMCP transport sub-app is mounted at the root.

The Starlette TestClient lets us assert behavior without actually
binding a port.
"""
from __future__ import annotations

import contextlib
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from mcm_engine.config import MCMConfig
from mcm_engine.server import MCMServer
from mcm_engine.transport import build_asgi_app


@pytest.fixture
def server(tmp_path):
    cfg = MCMConfig(
        project_name="transport-test",
        db_path=str(tmp_path / "transport.db"),
    )
    return MCMServer(cfg, project_root=tmp_path)


@pytest.fixture
def app(server):
    return build_asgi_app(server, transport="sse")


def test_healthz_returns_200(app):
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_readyz_returns_200_when_all_adapters_ok(app):
    client = TestClient(app)
    response = client.get("/readyz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["storage"] == "ok"
    assert body["checks"]["counters"] == "ok"
    assert body["checks"]["search"] == "ok"


def test_readyz_reports_per_adapter_failure(server):
    """If one adapter's probe throws, /readyz reports it and returns 503
    while other adapters still appear in the body."""

    class _BrokenSearch:
        def search(self, *args, **kwargs):
            raise RuntimeError("synthetic search failure")

    server.ctx.search = _BrokenSearch()
    app = build_asgi_app(server, transport="sse")
    client = TestClient(app)
    response = client.get("/readyz")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["checks"]["storage"] == "ok"
    assert body["checks"]["counters"] == "ok"
    assert "error" in body["checks"]["search"]


def test_unknown_transport_rejected(server):
    with pytest.raises(ValueError, match="unknown transport"):
        build_asgi_app(server, transport="grpc")


def test_streamable_http_transport_mounts(server):
    """The 'streamable-http' transport variant is the other supported
    string. Smoke test: build doesn't raise; /healthz still answers."""
    app = build_asgi_app(server, transport="streamable-http")
    client = TestClient(app)
    response = client.get("/healthz")
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Inner-lifespan resolution. Some FastMCP transports (streamable-http) expose
# `lifespan = None` on the sub-app and keep the real lifespan on
# `router.lifespan_context`. build_asgi_app must run that inner lifespan, or
# the transport's session-manager task group never starts and requests fail
# at runtime ("task group is not initialized"). These fakes drive only the
# outer Starlette lifespan (via `with TestClient(app)`), so the mounted app's
# ASGI __call__ is never invoked.
# ---------------------------------------------------------------------------


class _FakeMCPApp:
    """Minimal ASGI-shaped stand-in for a FastMCP transport sub-app."""

    def __init__(self, *, lifespan, lifespan_context):
        self.lifespan = lifespan
        self.router = SimpleNamespace(lifespan_context=lifespan_context)

    async def __call__(self, scope, receive, send):  # pragma: no cover
        pass


def _record_lifespan(log, tag):
    @contextlib.asynccontextmanager
    async def _cm(app):
        log.append(f"{tag}:start")
        yield
        log.append(f"{tag}:stop")

    return _cm


def test_inner_lifespan_falls_back_to_router_lifespan_context(server, monkeypatch):
    """When mcp_app.lifespan is None, the wrapper must run the inner lifespan
    found on router.lifespan_context (the streamable-http shape)."""
    log: list[str] = []
    fake = _FakeMCPApp(lifespan=None, lifespan_context=_record_lifespan(log, "inner"))
    monkeypatch.setattr(server.mcp, "sse_app", lambda: fake)

    app = build_asgi_app(server, transport="sse")
    with TestClient(app):
        pass

    assert log == ["inner:start", "inner:stop"], (
        "router.lifespan_context fallback did not run — the inner MCP lifespan "
        "was skipped"
    )


def test_inner_lifespan_prefers_direct_lifespan_attr(server, monkeypatch):
    """When mcp_app.lifespan is set, it wins over router.lifespan_context."""
    log: list[str] = []
    fake = _FakeMCPApp(
        lifespan=_record_lifespan(log, "direct"),
        lifespan_context=_record_lifespan(log, "router"),
    )
    monkeypatch.setattr(server.mcp, "sse_app", lambda: fake)

    app = build_asgi_app(server, transport="sse")
    with TestClient(app):
        pass

    assert log == ["direct:start", "direct:stop"]


def test_lifespan_noop_safe_when_no_inner_lifespan(server, monkeypatch):
    """With neither lifespan nor router.lifespan_context, startup/shutdown
    still succeed (the wrapper just yields)."""
    fake = _FakeMCPApp(lifespan=None, lifespan_context=None)
    monkeypatch.setattr(server.mcp, "sse_app", lambda: fake)

    app = build_asgi_app(server, transport="sse")
    with TestClient(app):
        pass  # entering + exiting the lifespan must not raise


# ---------------------------------------------------------------------------
# DNS-rebinding allow-list must track the real bind host (Invalid Host header
# regression): FastMCP auto-enables a localhost-only allow-list, so serving on
# a LAN address rejected every non-loopback client with 421 until serve()
# re-derived the allow-list from the bind host.
# ---------------------------------------------------------------------------
from mcm_engine.transport import (  # noqa: E402
    _configure_transport_security,
    _host_pattern,
)


@pytest.mark.parametrize(
    "value,expected",
    [
        ("192.168.8.88", "192.168.8.88:*"),
        ("192.168.8.88:8080", "192.168.8.88:8080"),
        ("host.local:*", "host.local:*"),
        ("[::1]", "[::1]:*"),
        ("[::1]:8080", "[::1]:8080"),
        ("  ", None),
    ],
)
def test_host_pattern_normalization(value, expected):
    assert _host_pattern(value) == expected


def test_configure_security_allows_explicit_lan_host(server):
    _configure_transport_security(server, host="0.0.0.0", allowed_hosts=["192.168.8.88"])
    ts = server.mcp.settings.transport_security
    assert ts.enable_dns_rebinding_protection is True
    assert "192.168.8.88:*" in ts.allowed_hosts
    # loopback stays allowed; unrelated hosts do not.
    assert "127.0.0.1:*" in ts.allowed_hosts
    assert "evil.example.com:*" not in ts.allowed_hosts


def test_configure_security_allows_concrete_bind_host(server):
    _configure_transport_security(server, host="192.168.8.88")
    ts = server.mcp.settings.transport_security
    assert "192.168.8.88:*" in ts.allowed_hosts


def test_configure_security_can_disable(server):
    _configure_transport_security(server, host="0.0.0.0", enable=False)
    ts = server.mcp.settings.transport_security
    assert ts.enable_dns_rebinding_protection is False
