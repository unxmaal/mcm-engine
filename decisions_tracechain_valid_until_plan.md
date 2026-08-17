# Plan: First-class decisions, trace_chain, and valid_until (mcm-engine)

## Context

Reviewing the semantica-agi/semantica project surfaced three ideas that fit mcm-engine's
"agent working memory" scope (the rest of semantica — RDF/OWL/SHACL/SPARQL/Datalog, graph
analytics, full bi-temporal — is a regulated-enterprise-data product and out of scope). A
grounding pass on the current code confirmed the gaps:

1. **No first-class decisions.** `add_knowledge(kind=...)` is free-text; `kind="decision"`
   gets zero special handling. What shipped in v3.9.0 was `supersede_knowledge` + the
   `supersedes` relation (audit trail) — supersession lineage, not decision recording.
   Nothing captures *scenario -> reasoning -> outcome* or lets you walk a decision's causal
   ancestry.
2. **Traversal caps at 1 hop everywhere.** `get_related` (tools/relations.py:162) is 1-hop
   both directions; `_spread_related_rules` (tools/search.py:248) is 1-hop RULE->RULE
   spreading activation. The storage primitives (`list_outgoing_relations` /
   `list_incoming_relations` / `iter_relations`, both adapters) already support N-hop; nobody
   walks past depth 1.
3. **No valid-time semantics; `rules.valid_until` is inert.** Confirmed by grep: `valid_until`
   is written only by supersede_rule (`= now()`) / unsupersede_rule (`= NULL`) and read only
   into `RuleRow` at hydration — no filter, rank, or tag ever consults it (scoring.py has zero
   references). `knowledge` has no `valid_until` column at all. So the `[STALE]` 90-day
   recency tag is the only time signal, and it conflates "old" with "possibly-wrong" — wrong
   for durable facts (a 2yr invariant is still true; a 2-day-old deploy fact is already
   stale).

Outcome: three sequenced, additive features. Decisions first (they lay down the causal edges),
then `trace_chain` (which walks them), then `valid_until` (an independent time-axis fix).
Diagnostic/lean posture preserved: no new entity table, no new runtime dependency, no full
bi-temporal.

Design decisions locked (via AskUserQuestion):
- **Decisions**: a dedicated `record_decision` tool (structured fields + `based_on`
  auto-linking), not just a documented convention.
- **Causal edge**: add a new `depends_on` verb to the sealed `RelationType`.
- **Expiry behavior**: soft — past-`valid_until` entries are tagged `[EXPIRED]` and
  deprioritized in rank, still returned (not hard-dropped like superseded).

---

## Phase 1 — First-class decisions (no schema change)

Rides the knowledge table (`kind="decision"`) + the relations graph. New verb + new tool.

**1a. Add the `depends_on` relation verb.**
- `tools/relations.py:21` — extend the sealed Literal:
  `RelationType = Literal["causes","contradicts","fixes","related","supersedes","depends_on"]`.
  `VALID_RELATIONS` is derived via `get_args` so it updates automatically; the MCP schema enum
  updates too (issue #49 pattern). The DB `relations.relation` column is free-text `TEXT`
  (schema.py:297) so no storage/migration change.
- `schema.py:297` — update the vocabulary comment to list `depends_on`.

**1b. New module `tools/decisions.py` -> `register_decisions_tools(mcp, ctx_or_db, tracker, project_name)`.**
Opens with `ctx = coerce_context(ctx_or_db)` then `storage = ctx.storage` (the standard module
preamble). One tool:

```python
record_decision(
    topic: str,
    scenario: str,
    reasoning: str,
    outcome: str,
    confidence: float = 0.0,
    based_on: list[str] | None = None,      # ["knowledge#41","error#7","rule#12","knowledge#88"]
    supersedes_decision: int | None = None, # id of a prior kind=decision knowledge row
    valid_until: str = "",                  # optional forward-dated expiry (Phase 3 field)
    tags: str = "",
    project: str = "",
) -> str
```
Behavior:
- Builds and inserts a `KnowledgeRow(kind="decision", ...)` via `storage.insert_knowledge`
  (reusing the exact shape at tools/knowledge.py:136-148): `summary=outcome`,
  `detail` = a structured block ("**Scenario:** ...\n\n**Reasoning:** ...\n\nConfidence: X"),
  `rationale=reasoning`, `tags`, `project=project or project_name`,
  `valid_until` (Phase 3). Duplicate handling is the existing `(topic, kind)` keying via
  `find_knowledge_by_topic_kind` — a re-recorded decision with the same topic updates in place.
- For each `based_on` ref: parse `"type#id"` with a small `_parse_ref` helper -> `(EntityType, int)`,
  validate with `storage.entry_exists`, then
  `storage.insert_relation(RelationRow(source_type=KNOWLEDGE, source_id=<new decision id>,
  target_type=<parsed>, target_id=<parsed>, relation="depends_on"))`. Direction: decision
  --[depends_on]--> evidence. Bad refs are reported, not fatal (skip + note in return string).
- If `supersedes_decision` given: reuse `storage.supersede_knowledge(new_id, old_id)` (the
  v3.9.0 method) so the old decision flips `status='superseded'` and a `supersedes` relation
  is recorded — identical semantics to `supersede_knowledge`.
- Returns the new id + a summary of links created / refs skipped.
- `find_similar_decisions` is NOT a new tool — it is `search(scope="knowledge")` filtered to
  decisions; note this in the docstring rather than adding surface.

**1c. Wire it** in `server.py`: import alongside :17-23, call
`register_decisions_tools(self.mcp, self.ctx, self.tracker, config.project_name)` in the
:173-212 block (next to `register_relations_tools` at :204). `record_decision` is a WRITE — do
NOT add to `READ_ONLY_TOOLS`.

**Reuses:** `coerce_context`, `storage.insert_knowledge`, `storage.insert_relation`,
`storage.entry_exists`, `storage.supersede_knowledge`, `KnowledgeRow`/`RelationRow`,
`EntityType`. **No new SQL site** (all via existing storage methods) -> seam inventory
untouched by Phase 1.

**Tests** (`tests/test_decisions.py`): record_decision stores `kind='decision'`; `based_on`
creates N `depends_on` relations (verify via `list_outgoing_relations`); invalid ref is skipped
with a note; `supersedes_decision` flips the old row to `superseded`; and a relations-vocab test
that `link_knowledge(..., "depends_on")` is now accepted.

---

## Phase 2 — `trace_chain` (N-hop directed walk, read-only)

Generalize the `_spread_related_rules` BFS (tools/search.py:248) from fixed-1-hop-RULE->RULE
to bounded N-hop, cross-entity-type, single-verb, directional. Lives with `get_related` in
`register_relations_tools` (tools/relations.py).

```python
trace_chain(
    entry_type: EntityTypeLiteral,
    entry_id: int,
    relation: RelationType | None = None,  # filter to one verb (e.g. "depends_on"); None = any
    direction: str = "outgoing",           # "outgoing" | "incoming" | "both"
    max_depth: int = 5,
) -> str
```
Implementation: validate type + `entry_exists`; BFS with a `seen` set of `(etype, id)` (cycle
guard, seeded with the root) and a queue of `(etype, id, depth)`. At each node, pull
`list_outgoing_relations` and/or `list_incoming_relations` per `direction`, filter by `relation`
if given, enqueue unseen neighbors at `depth+1` up to `max_depth`. Cross-type edges allowed
(decision --depends_on--> error, etc.), unlike the RULE-only spread. Render an indented tree
with relation labels and entry labels (reuse `_entry_label` from tools/relations.py). Bound
output with `MAX_NODES` (~200); if hit, emit an explicit truncation line (no silent cap).

**Wiring:** already covered by `register_relations_tools` at server.py:204. **Add
`"trace_chain"` to `READ_ONLY_TOOLS`** (tracker.py:61) — it is read-only, and omitting it would
let it advance the nudge/block machinery (same treatment as `get_related`, `session_metrics_report`).

**Reuses:** `list_outgoing_relations`/`list_incoming_relations`, `_entry_label`, `EntityType`,
`VALID_RELATIONS`. **No new SQL site** -> seam inventory untouched by Phase 2.

**Tests** (`tests/test_trace_chain.py`): build A--depends_on-->B--depends_on-->C; assert the walk
reaches C at depth 2 and stops at `max_depth=1`; `relation` filter excludes other verbs;
`direction` incoming/outgoing/both; a cycle A->B->A terminates; cross-type chain
(decision->error->rule) walks; and assert `"trace_chain" in SessionTracker.READ_ONLY_TOOLS`.

---

## Phase 3 — `valid_until` on knowledge, honored on both (schema v15 -> v16)

Give knowledge a forward-dated `valid_until` mirroring the rules column, and make search honor
it for BOTH entity types (the first-ever read of `rules.valid_until`). Soft tag+deprioritize.

**3a. Schema (both adapters).**
- `schema.py`: bump `CORE_VERSION = 15 -> 16` (:6). Add `valid_until TEXT` to the CORE_SCHEMA
  knowledge table beside status/superseded_by (:35-36). Add `_migrate_v15_to_v16` (guarded
  `_has_column` ALTER, template `_migrate_v13_to_v14` at :700-714) and register
  `(15, 16, _migrate_v15_to_v16)` in `_MIGRATIONS` (:737-753). The SQLite ALTER via
  `db.execute_write` is **+1** SQL site (schema.py 64 -> 65).
- `adapters/postgres/storage.py`: add `valid_until TIMESTAMPTZ` to the knowledge CREATE (~:88-90,
  kept OUT of the `tsv` generated column) and a guarded `DO $$ ... information_schema.columns ...
  ADD COLUMN` block (template = the v14 knowledge block at :315-327). DDL string literals add
  **0** `.execute` sites. `_OWNED_TABLES` unchanged (no new table).

**3b. Dataclass + hydration.**
- `backends/__init__.py`: `KnowledgeRow` gains `valid_until: Optional[datetime] = None`
  (after superseded_by, :141).
- `adapters/sqlite/storage.py` `_knowledge_from_row` (:55-76): add
  `valid_until=_parse_dt(_col(r, "valid_until"))` (exact template: `_rule_from_row` at :140).
- `adapters/postgres/storage.py` `_knowledge_from_row` (:583-604): add
  `valid_until=_as_dt(r.get("valid_until"))` (template: `_rule_from_row` at :659).

**3c. Write paths.**
- `add_knowledge` (tools/knowledge.py:73): add `valid_until: str = ""` param, pass into the
  `KnowledgeRow` and into the update path's `update_fields`.
- `record_decision` already carries `valid_until` (Phase 1b).
- Rules forward-date path: allow `set_rule_metadata` to set `valid_until` (it is already the
  rules-axis editor). Keeps supersede's `valid_until=now()` semantics intact — a superseded
  rule is dropped in search *before* the expiry check, so no double-tag.

**3d. Honor in search — soft tag + deprioritize.**
- `tools/search.py` `_score_and_format_knowledge` (:96-134): after hydration, compute
  `expired = valid_until is not None and valid_until < now` (reuse the tz-aware/naive handling
  in `_age_days` at :75-84 via a small `_is_expired` helper). If expired: append `" [EXPIRED]"`
  to the entry (beside the `[STALE]` tag at :122-126) and sink the score (e.g. `*= 0.1`) so it
  falls below live hits. Still return the entry (soft, not `None`).
- `tools/search.py` `_score_and_format_rule` (:137-203): same expiry tag+penalty. This is the
  first read of `rules.valid_until`. The superseded drop at :159-160 runs first, so
  supersede's `valid_until=now()` never reaches the expiry check.
- Keep the `[STALE]` recency tag unchanged — "stale by disuse" and "expired by validity" are now
  cleanly separated signals. search.py stays at 0 SQL sites (reads hydrated rows).

**3e. Seam inventory (lockstep, same commit).**
- `tests/test_seam_inventory.py` `EXPECTED_SQL_SITES_BY_FILE`: `schema.py` 64 -> 65 (:35).
  sqlite/postgres storage unchanged (+0). Add a `docs/seam-inventory.md` addendum documenting
  the 64->65 bump (templates: the v14/v15 addenda at seam-inventory.md:667-712).

**Tests:**
- `tests/test_schema.py`: clone `test_v13_to_v14_adds_knowledge_lifecycle` (:181-211) ->
  `test_v15_to_v16_adds_knowledge_valid_until` (stamp `('core', 15)`, assert column
  absent->present, version == `CORE_VERSION`); plus `test_fresh_install_has_knowledge_valid_until`.
- Storage round-trip: `KnowledgeRow.valid_until` persists + hydrates on both adapters
  (SQLite + Postgres-gated, DSN `postgresql://mcm:mcm@127.0.0.1:55432/mcm_test`).
- Ranking (`tests/test_search_valid_until.py`): active knowledge/rule with a PAST `valid_until`
  gets `[EXPIRED]` + sinks below a live hit but is still returned; FUTURE `valid_until` ranks
  normally; a superseded row is still dropped (no double-tag).
- Seam-inventory guard stays green after the count update.

---

## Versioning, config, rollout

- **One minor release, `3.11.0`** (v3.10.0 already shipped). Commit-boundaried:
  1. `depends_on` verb + `record_decision` (`tools/decisions.py`) + server wiring + tests
  2. `trace_chain` + `READ_ONLY_TOOLS` + tests
  3. `knowledge.valid_until` schema v16 (both adapters) + dataclass/hydration + seam inventory + migration/round-trip tests
  4. Honor `valid_until` in search (knowledge + rules) + `add_knowledge`/`set_rule_metadata` write paths + ranking tests
  5. CHANGELOG + version bump + docs
- **Config:** no new config needed — `valid_until` is per-entry, not global. `MetricsConfig`
  and everything else untouched.
- **Deploy:** schema v16 auto-creates on startup (guarded, both adapters); no manual migration.
  Basement pod picks it up whenever 3.11.0 is deployed; local SQLite works immediately.

## Explicitly NOT building

- No full bi-temporal: no `valid_from` axis (created_at already is recorded-time), no Allen
  interval algebra, no "as-of-T" replay.
- No generalized N-hop *neighbor expansion in search* — the KB graph is sparse (~few relations),
  so undirected multi-hop in ranking is noise. `trace_chain` is directed, single-verb, on-demand
  only.
- No new decision entity or table — decisions ride `knowledge.kind="decision"`.
- No graph analytics (centrality/community/link-prediction), no RDF/OWL/SHACL/SPARQL, no new
  runtime dependency.

## Verification

- `uv run pytest -q` (embedded SQLite). Then bring up `tests/docker-compose.yml`
  (Postgres 127.0.0.1:55432, mcm/mcm/mcm_test) and re-run for the pg-gated cross-adapter tests.
  `psql` is not on PATH here — use psycopg.
- **Manual e2e (both backends):** `record_decision(...)` with `based_on` refs, then
  `trace_chain("knowledge", <id>, relation="depends_on", max_depth=5)` — confirm the causal
  ancestry renders. `add_knowledge(..., valid_until=<past>)` then `search` — confirm `[EXPIRED]`
  appears and the row sinks below a live hit; a future `valid_until` ranks normally. Run against
  local SQLite and against the compose Postgres over MCP HTTP.
- Migration check: seed a v15 DB, run `migrate_core`, confirm `knowledge.valid_until` appears
  and `CORE_VERSION == 16`.

## Process caveats (from prior sessions)

- NEVER `git add -A` in this repo — it has swept the intentionally-untracked never-commit files
  (`mcm_c5_modernization_plan.md`, `rules/conventions/uv-for-all-python.md`) into commits.
  Stage explicit paths only.
- NEVER use backticks in `git commit -m "..."` (shell command-substitution mangles it) — use
  `git commit -F <file>`.
- `uv` for all Python — never system python3/pip/venv.
- Seam-inventory guard must be updated in the SAME commit as any new SQL site.
