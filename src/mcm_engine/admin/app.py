"""Admin plane HTTP app (issue #64, Phase 3) — standard library only.

A thin ``BaseHTTPRequestHandler`` over the pure logic in ``service.py``. No web
framework dependency: the admin container stays small and the only imports are
stdlib + the shared ``mcm_engine`` storage library.

Routes:
  GET  /                                  -> the grid UI (static HTML)
  GET  /api/rules?include_archived&min_importance&limit -> rules_payload JSON
  POST /api/rules/<id>/metadata  (JSON)   -> apply_metadata
"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import service

_STATIC = Path(__file__).parent / "static"
_METADATA_RE = re.compile(r"^/api/rules/(\d+)/metadata$")


def _load_static(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


def _load_index() -> str:
    return _load_static("index.html")


def _truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def make_handler(storage, *, index_html: str | None = None, graph_html: str | None = None):
    """Build a request handler class bound to ``storage``. The static pages are
    read once at construction (override in tests)."""
    html = index_html if index_html is not None else _load_index()
    graph = graph_html if graph_html is not None else _load_static("graph.html")

    class Handler(BaseHTTPRequestHandler):
        # Quiet by default — the container's stdout is for real events.
        def log_message(self, *args) -> None:  # noqa: D401
            pass

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, status: int, obj: dict) -> None:
            self._send(status, json.dumps(obj).encode("utf-8"), "application/json")

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html"):
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path in ("/graph", "/graph.html"):
                self._send(200, graph.encode("utf-8"), "text/html; charset=utf-8")
                return
            if parsed.path == "/api/graph":
                q = parse_qs(parsed.query)
                include_archived = _truthy(q.get("include_archived", ["0"])[0])
                try:
                    payload = service.graph_payload(
                        storage, include_archived=include_archived)
                except Exception as e:
                    self._json(500, {"error": str(e)})
                    return
                self._json(200, payload)
                return
            if parsed.path == "/api/rules":
                q = parse_qs(parsed.query)
                include_archived = _truthy(q.get("include_archived", ["0"])[0])
                try:
                    min_importance = int(q.get("min_importance", ["0"])[0] or 0)
                except ValueError:
                    min_importance = 0
                limit_raw = q.get("limit", [""])[0]
                limit = int(limit_raw) if limit_raw.isdigit() else None
                try:
                    payload = service.rules_payload(
                        storage,
                        include_archived=include_archived,
                        min_importance=min_importance,
                        limit=limit,
                    )
                except Exception as e:  # storage failure -> 500, don't crash the loop
                    self._json(500, {"error": str(e)})
                    return
                self._json(200, payload)
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            m = _METADATA_RE.match(parsed.path)
            if not m:
                self._json(404, {"error": "not found"})
                return
            rule_id = int(m.group(1))
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except json.JSONDecodeError:
                self._json(400, {"error": "invalid JSON body"})
                return

            importance = body.get("importance")
            # Tolerate a numeric string from a form field; leave validation to
            # the storage layer for anything it can't cleanly coerce.
            if isinstance(importance, str) and importance.strip():
                try:
                    importance = int(importance)
                except ValueError:
                    pass

            status, resp = service.apply_metadata(
                storage,
                rule_id,
                importance=importance,
                scope=body.get("scope"),
                kind=body.get("kind"),
                category=body.get("category"),
                actor=body.get("actor") or "admin-ui",
            )
            self._json(status, resp)

    return Handler


def make_server(storage, *, host: str = "127.0.0.1", port: int = 8090) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(storage))


def serve(config=None, *, host: str = "0.0.0.0", port: int = 8090) -> None:
    """Build the verified storage context and serve the admin UI forever.

    Uses ``build_verified_context`` so the admin plane honors the same
    authoritative-store binding as the MCP server — it will refuse to run
    against a store other than the pinned one."""
    from ..config import load_config
    from ..wiring import build_verified_context

    if config is None:
        config = load_config()
    ctx = build_verified_context(config)
    httpd = make_server(ctx.storage, host=host, port=port)
    identity = getattr(ctx.storage, "identity", "")
    print(f"mcm-engine admin UI on http://{host}:{port}  (store: {identity})", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
