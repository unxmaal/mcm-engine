# DNS-rebinding allow-list must register the BARE host, not just `host:*`

## Symptom
`streamable-http` (or `sse`) transport behind a reverse proxy / ingress / ALB
returns **`421 Invalid Host header`** for a Host that IS on the operator
allow-list (`--allowed-host` / `MCM_ALLOWED_HOSTS`). Bearer auth passes first
(an unauthenticated call 401s), so this is specifically Host validation. Only
the localhost defaults appear to enforce.

## Root cause (NOT an ordering bug)
The `serve()` ordering (configure security, then `build_asgi_app`) is correct,
and the mutated `settings.transport_security` DOES reach the session manager.
The real defect is the **pattern shape**. `mcp`'s
`TransportSecurityMiddleware._validate_host` accepts a Host only by:
1. exact string match (`host in allowed_hosts`), or
2. a `base:*` pattern where the actual Host is `base:<port>` (it checks
   `host.startswith(base + ":")`).

`transport.py::_host_pattern` normalizes every bare host to `host:*`. Behind a
proxy on **443/80 the forwarded `Host:` header carries no port** (e.g.
`Host: svc.internal`), so it matches neither branch — `host:*` requires a
port — and every request 421s. The allow-list settings look correct in
introspection; only a live request through the middleware reveals it.

## Fix
In `_configure_transport_security`, register **both** forms for each allowed
host: the `host:*` port-wildcard AND the bare `host` (exact match). See
`src/mcm_engine/transport.py`. Verified with a live Starlette `TestClient`
request (`tests/test_transport.py::test_streamable_http_accepts_allowlisted_host`):
port-less and explicit-port allowed Hosts both pass; an unlisted Host still 421s.

## Debugging lesson
Introspecting `settings.transport_security.allowed_hosts` is NOT sufficient —
it showed the host present while live requests still 421'd. Only a request
routed through the actual middleware (TestClient POST /mcp with a `Host`
header) exposes the matcher semantics. Test the middleware, not the settings.

Refines the earlier "FastMCP localhost-only 421" rule. Issue #92.
