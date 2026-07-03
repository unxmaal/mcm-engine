# mcm-engine v3 — Foundation Quad (+ SOTA-informed roadmap)

> STATUS: FINAL. Save as `mcm_v3_plan.md` in the mcm-engine repo root (next to
> `mcm2-scaling-plan.md`). Scope: Foundation **quad** — Piece A includes supersession.

## Context

mcm-engine serves two personas with opposite needs — an EXECUTOR (exact, authoritative,
coherent, deterministic) and a LIBRARIAN (recall, truth-decay, reviewable). Several gaps are
executor-constraints wrongly applied to librarian jobs. Five "wild ideas" lenses converged on a
small foundation; a SOTA survey (below) then validated the direction and sharpened it.

**Where mcm sits vs the field:** its no-RAG/FTS bet has frontier company (MemoryOS is FTS5-primary;
Memobase is embedding-free; BM25 beats vectors on code queries). Its real gaps —
correctness≠popularity, invalidation, dedup, conflict, reviewability — are the field's *open*
problems, and the best published answers are largely deterministic / embedding-free. Things SOTA
has solved that mcm lacks: temporal invalidation/supersession (Graphiti), deterministic dedup
(Graphiti MinHash/LSH), git-native versioning (Letta). On *use-tracking* mcm is unusual in even
having a primitive — though Piece B deliberately converts it from a gate to telemetry (see B).

---

## Implementation discipline (non-negotiable)

This plan describes *what*; these govern *how*. Follow the mcm-engine project contract:

- **TDD, test-first, every piece.** Write the failing test FIRST (templates named per piece), run
  it, watch it fail for the expected reason (red), THEN implement to green. No production code
  before a red test. (KB rule: "TDD means test-first.")
- **KB-first, always.** Before touching any seam: `search` / `read_rule` the KB — it is
  authoritative for project specifics, NOT your training. On any error: `report_error` BEFORE
  attempting a fix. The moment a test goes green: `add_rule` immediately with `file_path` (do NOT
  batch — context pressure drops deferred captures). Capture findings with `add_knowledge` as you go.
- **Re-verify seams.** Every `file:line` here was captured at a point in time — confirm against the
  live tree before editing.
- **Migrations touch BOTH adapters** (SQLite `schema.py`+`CORE_VERSION`; Postgres `_DDL_STATEMENTS`
  + idempotent `DO $$` ALTER), with storage-conformance coverage for both.
- **No LLM in the write path** (Mem0 lesson) and **no destructive deletes** — soft-expire /
  quarantine, reversible via `restore_rule`.

---

## Workflow: GitHub issues + branch-per-piece

- **One issue per piece.** Piece B is the already-filed **#19** (fail-open) — reuse it. Open two
  NEW issues: Piece A (correctness + supersession + `report_outcome`) and Piece C (DB→git mirror);
  each issue body = that piece's section here. (#20 source_of_truth is separate.)
- **Branch-per-issue, created BEFORE any edit** (`feat/<n>-correctness-axis`, `fix/19-fail-open`,
  `feat/<n>-git-review-mirror`), off `main` — never commit to `main`. Cut the branch first (no
  stash/pop churn).
- **One PR per branch**, `Closes #<n>`. TDD within each branch. Commit trailer + PR footer per repo
  convention.

---

## Prior art / SOTA (research synthesis)

- **Deterministic memory is viable at the frontier** (MemoryOS FTS5-primary, Memobase no-embeddings,
  BM25>vectors for code). Recall compensations: LLM query-expansion, frecency, advisory embedding
  fallback (few/no-hit only) — never the primary index.
- **Outcome-coupled confidence is SOTA** (Voyager execute-to-verify, VerificAgent, ExpeL
  UPVOTE/DOWNVOTE). Caveat (Experience-Following / self-reinforcing-error): pass/fail must adjust
  ranking **with decay/exploration, never a hard ban**.
- **Don't put an LLM in the write path** (Mem0's LLM ADD/UPDATE/DELETE is dead code in their repo).
- **Invalidation = soft-expire, resolved deterministically** (Graphiti; "Don't Ask the LLM to Track
  Freshness" → highest-version-wins, no model call).
- **Deterministic dedup** (Graphiti MinHash/LSH). **Git-native memory** (Letta commit-per-write).
- **Conflict** — surface it, don't silent last-write-wins.
- **Poisoning (new axis)** — provenance labels *in the prompt are ignored* → enforce in retrieval
  code; isolate untrusted memory.
- **Ranking** — Mem0 additive-hybrid (sigmoid-normalized bm25) + Generative-Agents normalized
  weighted sum, both port onto SQLite FTS5 `bm25()` with no embeddings.

Sources: Graphiti (2501.13956), Mem0 (mem0ai/mem0), Letta memory_repo, Generative Agents
(2304.03442), ExpeL (2308.10144), VerificAgent (2506.02539), Experience-Following (2505.16067),
"Don't Ask the LLM to Track Freshness" (2606.01435), defense taxonomy (2606.04329), MemoryOS,
Memobase.

---

## Piece A — Correctness axis + supersession + `report_outcome` (gap 3)

**Goal:** decouple TRUTH from popularity — a correctness signal moved by *independent* outcomes,
plus non-destructive invalidation of superseded rules.

**A1 — Correctness signal:**
- Append-only **`rule_outcomes`** `(id, rule_id, actor, passed, at)` — **store `(actor, passed)`
  only; do NOT persist a trust weight** (that would snapshot trust at write time). Apply the current
  `actor→weight` at aggregation/rank time so retuning the trust map reweights history (late-binding,
  chosen deliberately). Fast-path implication: keep a cheap per-rule rollup (recomputable) rather
  than baking weight into a counter.
- **AUTHOR≠JUDGE guard (load-bearing).** An outcome where `actor == rule author` is
  self-certification — the model agreeing with itself — and must be **log-only / ~0 weight** so it
  cannot move correctness the way an independent actor's report can. Trust keys on the *author≠judge
  relationship*, not identity alone. Without this, the correctness axis inherits the exact
  "used-a-lot-because-echoed vs because-right" confound that popularity already has.
- **Trust weighting:** config `actor→weight` map (default 1.0), anchored on `resolve_actor`
  (`principal.py:46`); `"nobody"`/unknown = low weight or log-only.
- **Ranking:** reformulate `compose_rank` (`scoring.py:51`) to additive-hybrid — sigmoid-normalize
  FTS5 `bm25()`, additive-combine with normalized hit/reinforcement/**correctness**/recency terms.
  Fed from `tools/search.py:134`.
- **Write path (must be ONE transaction on both adapters — `storage.transaction()`):**
  `resolve_actor` → author≠judge check → `counters`/rollup update → `rule_outcomes` row →
  `insert_rule_event("outcome")`. Partial failure must not drift counters. **No LLM here** (Mem0).
- **v1 policy:** a failing outcome only *adjusts ranking with decay/exploration*; **no hard
  auto-ban**. Auto-quarantine deferred.

**A2 — Supersession / invalidation (embedding-free, deterministic):**
- Columns `valid_until` (nullable), `superseded_by` (rule id), `status` (active|superseded), same
  v8→v9 migration as A1.
- **Never delete:** on supersession set `valid_until = now()`, `superseded_by = <new id>`,
  `status = superseded`. Retrieval defaults to `valid_until IS NULL OR valid_until > now()`;
  `--as-of <ts>` surfaces history.
- **Freshness deterministic** (highest-version/timestamp-wins, no LLM).
- **v1 boundary: explicit supersession only** — a `supersede_rule(old_id, new_id)` op (or a
  `supersedes` arg on `add_rule`) writes the link + a `superseded` `rule_events` row. Because
  supersession is explicit, false-invalidation isn't a v1 risk. **Do NOT import Graphiti's
  interval-overlap guard here** — it's near-vacuous for mcm: two live rules are both `[created, ∞)`
  and always overlap, so the check never skips the live→live case; it only matters once Phase-2
  automatic contradiction detection produces varying validity intervals.

**Seams:** `schema.py` CORE_SCHEMA + `_migrate_v8_to_v9` (template `_migrate_v7_to_v8:454`) + bump
`CORE_VERSION` 8→9; postgres `_DDL_STATEMENTS` + idempotent `DO $$ … ALTER` (:169) + `_OWNED_TABLES`;
`RuleRow` (`backends/__init__.py:150`) + both `_rule_from_row`; `StorageBackend` + both adapters
(`record_outcome`, `supersede_rule`, wrapped in `transaction()`); register `report_outcome` (+
`supersede_rule`) in `register_rules_tools` (`tools/rules.py:247`); default-filter superseded rows
in the search path. Rule authorship: use `created_by` on `RuleRow` for the author≠judge check.
**Tests:** `test_reinforcement.py` (FakeMCP), `test_scoring.py`, `test_schema.py` (v8→v9), a
self-report test (author==actor → correctness unmoved), a supersession test (superseded rule leaves
default search, returns under `--as-of`), transaction-atomicity test, conformance.

---

## Piece B — Fail-open consultation-gap recorder (gap 2, closes #19)

**Goal:** the PreToolUse hook never blocks; it records a `consultation_gap` event instead. **Name
it honestly:** after this change the primitive is *accountability telemetry, not enforcement with
teeth* — the docstring must say "consultation-gap recorder," so future readers don't assume a gate.
**Finding:** the hook (`hooks/mcp_enforcement.py`) contacts no backend — the catch-22 is purely a
hard `exit 2`, so **no liveness probe is needed**.
**Design:** the sole block branch `_decide():301-319` (`return 2`) → return `0` with a softer
message (still counts the mutation); `main():387-396` derives `action="consultation_gap"` and
`_append_event` to `.claude/mcp-enforcement-events.jsonl`. Update block-message + docstring; keep
WARN + counting as the accountability substrate.
**Tests:** rewrite the block assertions in `test_hook_mcp_enforcement.py` to `exit 0` + a
`consultation_gap` event; mirror `test_block_event_is_logged`.

---

## Piece C — DB→git review mirror (gap 5)

**Goal:** one-way replay of the DB into a git repo of markdown — audit/review only, never
authoritative, never writes `rules_paths`.
**Decision — v1 = current-state snapshot** (historical bodies aren't stored per-event; per-event
commits would misrepresent blame). Enumerate via `iter_entries(EntityType.RULE)`, render with
`_generate_rule_content`+`_slugify` (`tools/rules.py:97`,`:27`) to `<out>/<category>/<slug>.md`,
commit.
**Respect A2 (C depends on A2, not independent):** `iter_entries` enumerates superseded rows — the
mirror must **filter to active** (`valid_until IS NULL`/`status=active`) by default, OR mirror
history under a `superseded/` subtree. Never present dead rules as authoritative markdown.
**git guards:** run with an explicit identity (`git -c user.email=… -c user.name=…`, since a fresh
container has none) and handle the empty case (check for changes, or `commit --allow-empty`), so a
no-change run doesn't error.
**Seams:** new `cmd_export_mirror` in `cli.py`, registered like `cmd_migrate`; storage via
`open_storage(dsn)`; `--out <git-dir>`; shell to `git` via `subprocess` (no git dep).
**One-way safety:** read methods only + writes into the external git dir → structurally cannot
trigger the `sync_rules` orphan-archive sweep (`tools/rules.py:743`).
**SOTA note:** Letta's git-native write path (commit-per-write + per-agent lock) is the Phase-2
evolution — it would also solve concurrent-write clobber.
**Tests:** `test_cli_ingest.py` style; a mirror test (temp DB → temp git dir → files + commit +
`rules_paths` untouched + DB unchanged); a superseded-rule-excluded test; a no-change / no-identity
guard test.

---

## Sequencing & dependencies
B (smallest; closes #19) and A are independent. **C depends on A2** (its renderer must honor the
supersession filter), so land A before C. Order: **B → A → C**.

## Verification
- **A:** `pytest tests/test_reinforcement.py tests/test_scoring.py tests/test_schema.py`; live:
  `report_outcome` from an independent actor moves correctness + writes `rule_outcomes` +
  `rule_events` atomically; a self-report (`actor==author`) does NOT; `supersede_rule` → old rule
  leaves default results, `--as-of` still finds it; conformance on SQLite + Postgres.
- **B:** `pytest tests/test_hook_mcp_enforcement.py`; live: exceed threshold with backend down →
  edits proceed, `exit 0`, a `consultation_gap` line in the jsonl.
- **C:** the mirror test; live: `mcm-engine export-mirror --from <dsn> --out /tmp/mirror` →
  `git log`/`git blame` work, superseded rules absent from the active tree, `rules_paths` untouched,
  DB unchanged; a second run with no changes doesn't error.

---

## Deferred (Phase 2+), with concrete SOTA mechanisms
- **Automatic contradiction detection** feeding A2 (where the interval-overlap guard finally earns
  its place, once validity intervals vary).
- **Deterministic dedup (gap 4):** Graphiti MinHash/LSH (Jaccard≥0.9, entropy-gate). No embeddings.
- **Background consolidation "sleep" pass (gaps 3/4):** importance/merge/decay/evict; propose-only.
- **Conflict typing + surface on the write path (gap 5).**
- **Poisoning defense (new axis):** delimit untrusted memory + enforce trust/provenance in
  retrieval code, not the prompt; A-MemGuard pre-use consensus.
- **Ambient recall daemon + spreading activation (gap 1):** hook-as-muse; one-hop over
  `get_related`; advisory embedding fallback (few/no-hit only) as the one bounded no-RAG exception.
- **Git-native write path w/ locking (Letta):** evolution of Piece C.
- **Token ledger:** denominate value in tokens-saved; surface in `session_start`.
