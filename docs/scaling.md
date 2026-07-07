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

## Layer B — Postgres connection model

**Today (correctness floor, shipped):** each adapter holds one psycopg
connection, and every public method is serialized on a per-instance re-entrant
lock, with `transaction()` holding it across the whole block
(`adapters/postgres/_concurrency.py`). This closes the audit's H1/H3 by making
the shared connection safe under any threading — it is the same discipline the
SQLite `KnowledgeDB` uses. Because the event loop already serializes tools, this
lock is almost never contended today; it is defense-in-depth for the moment a
tool is offloaded to a thread (Layer C) or made async.

**The scaling target (not yet built): a per-pod `psycopg_pool.ConnectionPool`.**
Each operation borrows a connection; `transaction()` holds one across its block
(`pool.connection()` commits on clean exit, rolls back on exception — verified in
the library source). Benefits over the lock:

- removes H1/H3 **by construction** (no shared `_tx_depth`);
- gives the streaming `iter_*` cursors their own connections instead of sharing
  one;
- lets Layer C run tools on threads, each with its own connection;
- fronted by **RDS Proxy / PgBouncer** to multiplex the aggregate connection
  count from many pods.

The pool should land **together with Layer C** (there is no intra-pod
concurrency to exploit until then) and be validated against a real Postgres — it
is a mechanical but broad rewrite of the adapters' connection handling.

## Layer C — Per-pod throughput: keep the event loop unblocked

With horizontal scaling, the remaining per-pod risk is a single slow tool
stalling every client on the pod. Levers, cheapest first:

1. **Keep tools fast / bounded.** Caps already exist (`MCM_SIFT_MAX_SPANS`, the
   #79 search fix). Prefer more, smaller tool calls over one big one.
2. **Offload the few CPU-heavy tools** (`sift_candidates`, `consolidation_report`)
   to a threadpool (`anyio.to_thread`) so they don't hold the loop. This is safe
   **only with Layer B's pool** (each offloaded thread needs its own connection);
   attempting it against the single shared connection is exactly the hazard the
   Layer B lock guards.
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
| Postgres shared connection (H1/H3) | Fixed (per-adapter serialization lock); pool is the scaling successor |
| Transport out-of-band commits (H3) | Fixed (routed through the storage lock) |
| Per-pod throughput (heavy tools) | Open — Layer C (offload + pool) |
| Provenance ContextVar (M2) | Verified working (Starlette 0.52.1) + guarded by test |
| Horizontal scale | Pods + HPA + session affinity + RDS Proxy |
