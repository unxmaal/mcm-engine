# Tool-layer DB access must borrow via storage.transaction(), never reach storage._conn

## Rule
Under the Postgres connection pool (issue #83), `storage._conn` resolves to a
live connection ONLY inside a borrowed adapter method or a `storage.transaction()`
block (it reads the `_active_conn` contextvar). A tool-layer function (anything
under `src/mcm_engine/tools/`) that touches `storage._conn` outside such a block
raises "no active Postgres connection on this call-chain" — it does not return
None. So the old `conn = getattr(storage, "_conn", None); if conn is None` guard
is broken under the pool (issue #98).

For raw SQL from the tool layer:
- Wrap the SELECT/INSERT/DELETE in `with storage.transaction():` (borrows one
  connection, binds it so `storage._conn` resolves inside, commits on clean exit,
  rolls back on exception). Do NOT call `conn.commit()`/`conn.rollback()` yourself.
- Detect the backend by `storage.identity.kind` (`"postgres"` / `"sqlite"`), NOT
  by the truthiness of `storage._conn`.

## Reference
`tools/knowledge.py` kb_recall (fixed, issue #98); same borrow pattern as
`tools/rules.py` restore_rule / sync_rules. Regression: `tests/test_kb_recall.py`
(postgres-gated). PR #87 fixed the adapter's own callsites; kb_recall was the last
tool-layer `storage._conn` reach.
