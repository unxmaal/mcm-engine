# Capability flags — honest degradation

This document explains the `Capability` enum in `mcm_engine.backends`,
how adapters declare what they support, and how callers should probe
before invoking capability-gated functionality. Designed alongside
docs/contract-versioning.md as the escape hatch for adding methods
without bumping `CONTRACT_VERSION`.

## The contract

Every adapter declares:

```python
class MyAdapter:
    CONTRACT_VERSION: int = 1
    capabilities: set[Capability] = {Capability.VECTOR_SEARCH}
```

Callers probe before invoking:

```python
if Capability.VECTOR_SEARCH in search.capabilities:
    hits = search.search_vector(embedding, limit=10)
else:
    # Honest degradation — fall back to lexical search and tell the user.
    hits = search.search(query, limit=10)
    if user_requested_semantic_match:
        log.warning("vector search not available; using lexical fallback")
```

The principle: **adapters that don't support a feature MUST NOT lie
about it**. Better to degrade explicitly than to surface a confusing
runtime error or, worse, silently produce wrong results.

## Defined flags (v1)

### `Capability.VECTOR_SEARCH`

The adapter accepts an embedding vector and returns dense-vector
similarity results.

- **Implies:** a `search_vector(vector, *, limit, ...)` method exists.
- **Declared by:** no v1 reference adapter. Slated for Phase 3+ when
  a pgvector- or opensearch-knn-backed adapter ships.
- **Why not always on:** SQLite has no vector type; FTS5 is lexical.
  OpenSearch *can* do knn but our v1 adapter doesn't expose it (the
  storage rows don't yet carry pre-computed embeddings).
- **Honest fallback:** plain lexical `search(query, ...)`.

### `Capability.EMBEDDING_PROVIDER`

The adapter generates embedding vectors itself (calls an embedding
model on text and returns vectors), rather than requiring the caller
to pre-compute them.

- **Implies:** an `embed(text) -> list[float]` method exists.
- **Declared by:** none in v1 — explicitly deferred per MCM2-16.
  The embedding model choice is a separate pluggable axis (Sentence
  Transformers vs OpenAI vs local Ollama vs ...).
- **Honest fallback:** caller computes embeddings out-of-band.

### `Capability.DURABLE_SESSION`

A `SessionStore` persists tracker state across process restarts.

- **Implies:** `load_state(key)` returns the previously-saved dict
  even after the process restarts.
- **Declared by:** none in v1. A future Redis-backed SessionStore
  would declare it.
- **NOT declared by:** `InMemorySession` (the embedded reference)
  intentionally lacks this — state lives in-process per OQ-5.
- **Honest fallback:** start every session with empty tracker state.

## Why an enum and not a method on the Protocol

Two alternatives we rejected:

1. **A `has_vector_search() -> bool` method on each Protocol.** Forces
   every adapter to implement N feature-probe methods even when most
   are obviously false. The enum is a single declarative `set`.

2. **Optional methods on the Protocol via `hasattr`.** Duck-typing
   feature detection makes the contract implicit and untestable. The
   enum is checkable — `Capability.VECTOR_SEARCH in adapter.capabilities`
   is a real boolean, and the conformance suite can assert what each
   reference adapter declares.

## Conformance — what the test suite asserts

`tests/test_capabilities.py` asserts:

- The Capability enum carries the three named flags.
- Values are lowercase canonical strings (matches StrEnum convention).
- Each reference adapter declares the *correct* capability set for
  what it actually does — no false claims.

When a future adapter adds VECTOR_SEARCH, the test for that adapter
asserts the flag is present AND that `search_vector(...)` returns
something sensible on a seeded fixture. Adding a flag without the
method is a bug; adding a method without the flag is a bug. The
conformance suite is the place that links the two.

## Adding a new capability

1. Add the enum member with a docstring describing what it implies.
2. Document the implied Protocol method shape (in the relevant
   Protocol's docstring or this file).
3. Add an opt-in conformance test in `mcm_engine.testing.conformance`
   that skips for adapters without the flag and asserts the contract
   when present.
4. Update `docs/contract-versioning.md` to confirm the addition
   doesn't bump `CONTRACT_VERSION` — capabilities are exactly the
   mechanism that lets us avoid that.

## When to bump `CONTRACT_VERSION` anyway

If a capability becomes mandatory (e.g., we decide ALL future
adapters must support DURABLE_SESSION), promoting it from a flag to a
required Protocol method IS a breaking change and bumps the version.
That's the boundary the version exists to mark.
