# Scaling mcm-engine (EKS, hundreds–thousands of clients)

This is the architecture for running the daemon on a horizontally-scaled cluster
serving many concurrent human and agent clients. It also records the reasoning
behind the concurrency work in issue #83 so the next changes build on a shared
picture rather than rediscovering it.

## The one fact that drives everything

**FastMCP (mcp 1.26.0) runs synchronous tool handlers _inline on the event
loop_** — there is no threadpool (`server/fastmcp/utilities/func_metadata.py`
calls the sync function directly). Consequences:

- Within a single pod, tool calls are **serialized**: one runs to completion
  before the next starts. There is no intra-pod parallelism for tools.
- A **slow** tool (`sift_candidates`'s O(spans×rules) MinHash, a large `search`,
  `consolidation_report`) blocks the whole event loop — every client on that pod
  stalls until it returns.
- Most of the DB-layer data races the audit found are therefore **latent**: they
  need two handlers running at once, which inline dispatch prevents. The one
  genuinely-concurrent writer today is the **watcher cascade's `threading.Timer`
  threads** (files-authoritative mode only; disabled when
  `source_of_truth=database`).

So the scaling model is **horizontal**: many pods, each a serial event loop, with
durable state in Postgres. That choice dictates the three layers below.

## Layer A — Cross-pod correctness: session affinity (the hard part)

Two pieces of per-session state live **in-process**, not in Postgres:

1. The per-session `SessionTracker` (issue #83 / the `ScopedTracker`), keyed on
   the FastMCP `ServerSession` object.
2. **The MCP session transport itself.** `StreamableHTTPSessionManager` mints the
   `mcp-session-id` on the `initialize` request and keeps that session's transport
   in memory **on the pod that ran `initialize`**.

So a client's requests **must all reach the pod that minted its session**, or
both the tracker state and the transport are missing. Naive "hash by
`mcp-session-id`" routing does not achieve this: the first (`initialize`) request
carries no session-id header, so it lands somewhere, mints an id, and consistent-
hashing that id afterward may point at a _different_ pod.

There are only two correct shapes:

- **Sticky routing pinned to the minting pod.** The load balancer sets its own
  affinity cookie on the `initialize` response and honors it thereafter
  (e.g. nginx-ingress `nginx.ingress.kubernetes.io/affinity: cookie`). Correct
  **iff the MCP client echoes the cookie** across a session — verify your
  clients do before relying on it.
- **Externalize per-session state** (tracker, and ideally the transport session)
  to a shared store (Redis) so the tier is truly stateless and any pod serves any
  request. This is the robust end-state; it is a larger change (the SDK's session
  manager holds transport state in-memory) and is the recommended direction if
  cookie affinity can't be guaranteed.

> **Helm caveat:** `values.yaml` ships `replicaCount: 1`, and the chart README
> says raising it is "safe … durable state is in Postgres." That is true for
> **KB** state but **not** for governance/session state. Do not raise
> `replicaCount` above 1 until session affinity (or externalized state) is in
> place, or `ScopedTracker` fragments across pods and #83 silently regresses.

## Layer B — Postgres connection model (implemented)

**A per-pod `psycopg_pool.ConnectionPool`** (`adapters/postgres/_pool.py`).
Each adapter method borrows a connection for its duration and returns it;
`transaction()` binds one connection across its whole block. `pool.connection()`
commits on clean exit and rolls back on exception (psycopg's `with conn:`), so
there is no manual `_tx_depth` to corrupt. Mechanically, `self._conn` is a
property returning the current call-chain's borrowed connection, and a class
decorator (`pooled_adapter`) wraps each public non-generator method to borrow
one — so method bodies are unchanged. `build_context` opens **one** pool per DSN
and injects it into all three Postgres adapters, so a pod holds one bounded set
of connections. This:

- removes the audit's H1/H3 **by construction** (no shared transaction state);
- gives the streaming `iter_*` cursors their own connections;
- lets Layer C run tools on threads, each borrowing its own connection;
- is fronted by **RDS Proxy / PgBouncer** to multiplex the aggregate connection
  count from many pods (tune `max_size` in `make_pool` against your DB's limit).

Validated against a real Postgres: the adapter conformance suite (50 tests) plus
`tests/test_postgres_pool.py`, which asserts concurrent increments lose nothing,
a rolled-back transaction does not swallow a concurrent write (the old #83
corruption, now impossible), transaction isolation holds, and operations run in
parallel rather than serialized.

This replaced an interim per-connection serialization lock — the coarse lock was
correct but a throughput ceiling; the pool is the scaling-correct form.

## Layer C — Per-pod throughput: keep the event loop unblocked

With horizontal scaling, the remaining per-pod risk is a single slow tool
stalling every client on the pod. Levers, cheapest first:

1. **Keep tools fast / bounded.** Caps already exist (`MCM_SIFT_MAX_SPANS`, the
   #79 search fix). Prefer more, smaller tool calls over one big one.
2. **Offload the few CPU-heavy tools** (`sift_candidates`, `consolidation_report`)
   to a threadpool (`anyio.to_thread`) so they don't hold the loop. Layer B's
   pool is now in place, so each offloaded thread borrows its own connection —
   this is the piece that makes offloading safe. (The SQLite tier still
   serializes on the `KnowledgeDB` lock, which is correct for that single-node
   deployment.)
3. **Async tools** for I/O-bound handlers — the largest change; only if 1–2 are
   insufficient.

## Provenance attribution (audit M2) — verified working, guarded

The audit flagged that `BearerTokenMiddleware` is a Starlette `BaseHTTPMiddleware`
(runs the endpoint in a child anyio task), so the transport-principal contextvar
set in `dispatch` might not reach the tool handler — which would misattribute
authenticated writes to `MCM_ACTOR`/`nobody`. **Verified this is NOT the case on
Starlette 0.52.1:** a contextvar set BEFORE `call_next` propagates *down* to the
endpoint (only the reverse — endpoint→middleware — is lost). `resolve_actor()`
sees the bound principal correctly. `tests/test_principal_propagation.py` pins
this with the real `principal` module so a future Starlette bump that regresses
propagation is caught instead of silently misattributing writes. No code change
needed unless that guard ever fails, at which point the fix is a pure-ASGI
middleware.

## Summary

| Concern | Status |
| --- | --- |
| Per-session governance state (#83) | Fixed per-pod (`ScopedTracker`); **needs session affinity to hold across pods** |
| SQLite shared connection | Fixed (lock across `transaction()`) |
| Postgres shared connection (H1/H3) | Fixed (per-pod connection pool; validated against real PG) |
| Transport out-of-band commits (H3) | Fixed (each borrows its own pooled connection) |
| Per-pod throughput (heavy tools) | Open — Layer C (offload heavy tools; the pool is now in place to support it) |
| Provenance ContextVar (M2) | Verified working (Starlette 0.52.1) + guarded by test |
| Horizontal scale | Pods + HPA + session affinity + RDS Proxy |
