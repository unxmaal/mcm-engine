# Counter flush policy

This document explains the `CounterStore.flush()` contract, the
documented staleness window resolved in OQ-3, and how each adapter
implements flush semantics.

## The contract

```python
class CounterStore(Protocol):
    def increment(entity_type, entity_id, counter_name, by=1) -> None: ...
    def get(entity_type, entity_id) -> dict[str, Any]: ...
    def top_by(entity_type, counter_name, k) -> list[tuple[int, float]]: ...
    def flush() -> None: ...
    def last_flushed_snapshot(entity_type, entity_id) -> dict[str, Any]: ...
```

Two operations look like the same thing but aren't:

- **`get(...)`** — live counter values. Authoritative for the current
  in-flight session.
- **`last_flushed_snapshot(...)`** — the counter values from the most
  recent flush to durable storage. May lag `get` by up to the documented
  staleness window.

Why split: composing a ranked search result on every query is hot. The
composite scorer in `mcm_engine.scoring` consumes `last_flushed_snapshot`
to compose a hit's rank — that path can stay cheap even if `get` would
require a round-trip to a remote counter store.

## The staleness window — OQ-3 resolution

The window is **on the order of minutes**, not seconds and not hours.

- A few minutes is enough that a remote store can batch durable writes
  efficiently (1000s of increments → one bulk flush).
- A few minutes is short enough that "this entry is hot today" stays
  reflected in ranking without the user noticing the lag.
- Anything sub-second moves us back into per-write-fsync territory and
  loses the point of having a CounterStore at all.

Adapters that batch MUST document the actual window they aim for. The
engine doesn't enforce a specific number — `flush()` is called by ops
glue (a periodic task, a shutdown hook), and *how often* is the
operator's choice.

## Per-adapter behavior

### `SqliteCounters` (embedded reference)

- **Storage:** the entry row itself — `knowledge.hit_count`,
  `rules.reinforcement_count`, etc.
- **Flush model:** write-through. Every `increment(...)` immediately
  UPDATEs the row.
- **`flush()`:** no-op (no buffered state).
- **`last_flushed_snapshot()`:** identical to `get()` — there is no
  window.
- **Staleness window:** zero.

### `PostgresCounters` (write-through, second-adapter proof)

Same shape as SqliteCounters, just against Postgres tables.

- **Storage:** entry-row columns in Postgres (same six columns).
- **Flush model:** write-through. Every `increment(...)` UPDATEs the
  row in the same transaction.
- **`flush()`:** no-op.
- **`last_flushed_snapshot()`:** equals `get()`.
- **Staleness window:** zero.

PostgresCounters exists not because batched-Postgres is wrong — it's
fine — but because demonstrating *any* second adapter passing the same
`CounterConformance` proves the Protocol is product-agnostic. A
batched variant can be added later as a separate adapter when the
write rate justifies it.

### `RedisCounters` (off-row live store)

- **Storage:** one Redis ZSET per `(entity_type, counter_name)` —
  `ZINCRBY mcm:counters:knowledge:hit_count <by> <entity_id>`.
- **Flush model:** Redis IS the live store. Increments land in Redis
  directly with no batching needed (Redis is fast).
- **`flush()`:** no-op *for the read path*. A separate concern is
  periodically writing Redis values back to the durable
  `StorageBackend` entry-row columns for cold-start recovery — that
  belongs to ops glue (the "counter persistence daemon") and is *not*
  part of `CounterStore.flush()`. See "Durable write-back," below.
- **`last_flushed_snapshot()`:** returns the live Redis state — for
  this adapter, "live" and "most recently flushed" are the same value
  (Redis IS the latest commit point).
- **Staleness window:** zero between `get` and `last_flushed_snapshot`.
  The window is between Redis and the cold-recovery snapshot in
  StorageBackend; the operator picks how often the durable
  write-back daemon runs (e.g., every 2 minutes).

## Durable write-back (separate concern)

The "minutes window" applies between the **live counter store** (Redis)
and the **durable snapshot** (storage rows). This is *not* what
`CounterStore.flush()` does in the v1 RedisCounters. Reasoning:

- The hot read path (search ranking) reads from `CounterStore`. Latency
  of that path matters; a flush-to-storage-rows-during-read would
  defeat the point.
- Cold-recovery from a Redis crash needs *some* snapshot in storage.
  That snapshot can be minutes stale and the system is still useful —
  it just loses the last few minutes of hit-count growth, which the
  next user session will repopulate naturally.

The write-back daemon is left to a future phase. When it lands it will
look like:

```python
# Pseudo-code — a periodic ops task, not part of CounterStore.
while True:
    for et in EntityType:
        for cname in known_counters_for(et):
            for eid, value in counters.top_by(et, cname, k=large):
                storage.update_entity_counter(et, eid, cname, value)
    time.sleep(120)  # minutes window
```

`StorageBackend` does not yet expose `update_entity_counter`; that's a
contract addition that will land alongside the daemon when there's
demand. For now `mcm-engine migrate` is the durable-snapshot story
(periodic SQLite-snapshot exports during ops).

## Why no `staleness_seconds` parameter

We considered making `flush()` take a max-staleness argument or having
`last_flushed_snapshot` return a `(values, staleness_seconds)` tuple.
Rejected: the engine doesn't care about exact staleness when composing
ranks; the rank is dominated by other components (pinned, recency,
content). Adding a knob the engine never reads is over-engineering.

If a future use case needs guaranteed-fresh counters, it should call
`get(...)` (live), not `last_flushed_snapshot(...)` (flushed).

## What `CounterConformance` asserts

The shared `CounterConformance` test mixin checks shape and basic
guarantees:

- `isinstance(counters, CounterStore)` and `CONTRACT_VERSION` matches.
- `increment` then `get` returns the value.
- `last_hit_at` increment populates the field.
- Unknown counter names raise.
- `top_by` returns descending.
- Negative-knowledge rejects `hit_count` (only `pinned` is valid there).
- `flush()` returns None.
- `last_flushed_snapshot == get` (write-through equality). Adapters
  that intentionally batch flush-to-durable separately from
  CounterStore-live MAY override this single test if they choose to
  expose a stale-snapshot read path; today's three adapters all
  satisfy it as written.

The conformance is not — and shouldn't be — a measure of the actual
flush window. That's an ops property documented per-adapter above, not
a unit-testable invariant.
