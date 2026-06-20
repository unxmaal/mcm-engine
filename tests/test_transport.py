"""MCM2-20: HTTP/SSE transport — /healthz and /readyz probes.

Verifies the build_asgi_app wrapper:
  - /healthz is liveness; always 200 regardless of adapter state.
  - /readyz pings each adapter and reports per-adapter status.
  - The FastMCP transport sub-app is mounted at the root.

The Starlette TestClient lets us assert behavior without actually
binding a port.
"""
from __future__ import annotations

from pathlib import Path

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
