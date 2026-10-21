# mcm-engine Context Engineering Modernization Plan

**Revision note (2026-07-25):** The original draft targeted "Claude 5 generation models" and proposed a `model_generation` config that would gate behavior on the client's model, inferred automatically. That premise does not survive contact with the deployment or the code:

- **The model is unknowable to mcm.** The MCP `initialize` handshake carries only `clientInfo` (the client *application* name and version, e.g. `opencode`/`x.y.z`), never the driving model. mcm never reads even that; it uses the session object purely as an opaque identity key (`server.py:225-227`, `tracker.py:322-360`). There is no tool argument or env var carrying a model today. See `principal.py:47-63` for the one identity chain that exists (actor/provenance, not model).
- **The fleet is heterogeneous and non-Claude.** mcm-engine is part of the larger lodestone platform; most users run opencode, not Claude Code, and the corporate LiteLLM gateway fronts many models. A client may only ever see a gateway alias, not a concrete model. So model detection is impossible AND the target is not a single vendor's generation.

Conclusion: **drop model-awareness entirely.** The improvements below reward the same things every capable modern model rewards (expressive schemas over prose, minimal resident context, no repeated instructions, prescriptive-not-aggressive guidance), and Anthropic's own migration guidance for the current 4.6/4.7/4.8 and Sonnet 4.6 models already moved in this direction. The changes are safe across the fleet. The models that "less prescriptive" prompting could regress are the retired 3.x generation, which is out of the picture. Reversibility is provided by plain config flags, not a model gate.

**Context:** Modern frontier models perform better with fewer prescriptive constraints, no usage examples baked into tool descriptions, aggressive progressive disclosure, and no repeated instructions. mcm-engine's retrieval spine already aligns with this. The rules corpus, nudge system, tool descriptions, and handoff payload predate it and need updating.

**Ground rules for this work:**
- No behavior change ships without a measurement or a human review gate. Every behavioral change lands behind a config flag (defaulted so the change is reversible in one setting) and, where it affects nudges, behind new instrumentation so before/after can be compared. This replaces the original model gate.
- Model-agnostic. Nothing here detects, infers, or branches on the client model or vendor. If a concrete model ever needs recording, it can only come from an explicit operator-set signal, and even then it is attribution, never a behavior switch. Out of scope for this plan.
- mcm-engine stays narrowly focused on knowledge retrieval. Nothing here adds new product surface.
- Backward compatible: older models and existing team workflows keep working. New behavior is the default because it is broadly better, but every change is flag-reversible.

---

## Phase 0 — Instrumentation and flag scaffolding (prerequisite)

**Goal:** Make the later phases measurable and reversible, since the ground rule forbids unmeasured behavior changes and the nudge path currently has zero observability.

**Reality it corrects:** The MCP-side nudge system emits strings with no logging of whether or which nudge fired (`tracker.py` has no logging; `get_nudge` at `tracker.py:226-319` just returns text). The only nudge-adjacent event log that exists is the separate PreToolUse hook's JSONL (`hooks/mcp_enforcement.py`, `.claude/mcp-enforcement-events.jsonl`). So "measure before you change" is not possible today for nudges.

**Tasks:**
1. Add lightweight nudge instrumentation in `tracker.get_nudge` / `server.with_nudge`: record per-session which nudge type fired and whether it was a first fire or a repeat. Emit to the existing log path or a small JSONL, mirroring the enforcement-hook pattern.
2. Establish the config-flag convention in `NudgeConfig` (`config.py:20-53`) and its documented block (currently in `README.md:371-374`; note there is no `mcm-engine.yaml` in the repo, the live file the operator edits is outside it). Each behavioral change in later phases gets a boolean flag here.
3. No behavior change in this phase. It only adds visibility and the toggle surface.

**Acceptance:** A session's nudge fires are observable in logs (type, first-vs-repeat); config flags parse and default correctly; existing behavior unchanged.

---

## Phase 1 — Rules corpus audit (report only, no deletions)

**Goal:** Classify each rule as **FACT** (gotcha, environmental truth, business knowledge the model cannot infer, keep) or **CONSTRAINT** (defensive behavioral instruction written for older models, candidate for removal/demotion/rephrasing), and surface over-prescriptive or stale rules.

**Reality it corrects (three fixes to the original framing):**
- **Data source is the DB via MCP, not local files.** This checkout has exactly one rule file (`rules/deploy/dns-rebinding-portless-host.md`). The ~100-rule corpus lives in the Postgres pod, loaded from an out-of-repo tree (`scripts/import_rules.py:1-25`, `rules_categorization.md:8-25`). An audit script must read `list_rules` over MCP (the repo's own convention, which forbids bypassing MCP with direct DB access), not walk a `rules/` directory.
- **Reinforcement is not a separate table to join.** `reinforcement_count`, `hit_count`, and `last_hit_at` are columns on each rule row (`backends/__init__.py:150-187`, `schema.py:154-183`) and are already returned by `list_rules` (`tools/rules.py:645-672`, header at `:663-664`). No tracker-table join.
- **FACT vs CONSTRAINT overlaps the existing `kind` axis.** Issue #64 already landed a hierarchy: `kind` = `directive`|`fact`, `scope` = `universal`|`conditional`, `importance` = 0/1/2 (`hierarchy.py:22-35`), all settable via `set_rule_metadata` and readable via `list_rules`. The audit should classify/populate these existing axes, not invent a parallel taxonomy. FACT maps to `kind=fact`; CONSTRAINT maps to `kind=directive`.

**Tasks:**
1. Build `scripts/audit_rules.py` that calls MCP `list_rules` (with `include_archived`, no importance floor) and reads each rule's `kind`, `scope`, `importance`, `category`, `reinforcement_count`, `hit_count`, `last_hit_at`, `status`, and `content`.
2. Flag heuristically:
   - **Over-prescriptive CONSTRAINT candidates:** `kind=directive` rules that restate model defaults or use aggressive "never/always/must/do not" phrasing with no environmental fact attached, especially those with zero or near-zero reinforcement over their lifetime.
   - **Load-bearing FACTs:** `kind=fact` (or should-be-fact) rules with heavy reinforcement or recent hits.
   - **Mis-classified:** rows whose `kind` disagrees with their content (a gotcha marked `directive`, a behavioral nag marked `fact`).
   - **Stale:** old rules with no recent `last_hit_at`.
3. Emit `audit/rules_audit.md`: a table (rule id, category, current kind/scope/importance, hits, reinforcement, last hit, recommendation KEEP / RECLASSIFY / REPHRASE / DEMOTE / REVIEW) plus summary counts. Ambiguous entries are flagged REVIEW, not guessed.
4. Delete nothing. Output feeds human MR review; corpus edits route through the existing git-reviewed import flow.

**Acceptance:** Script runs against the live pod via `list_rules`, classifies 100% of returned rules (REVIEW allowed), report renders, spot-checked against 10 hand-labeled rules with at least 8/10 agreement. The report should also quantify how much of the corpus is already correctly `kind`-tagged versus needs reclassification.

---

## Phase 2 — Nudge hygiene: suppress repeats, keep first-surface

**Goal:** Stop re-emitting identical hygiene nudges within a session, while preserving first-surface delivery and the escalation backstop.

**Reality it corrects:** The original framing ("suppress reminders that restate a rule already surfaced") mischaracterizes what nudges are. Nudges are frequency- and counter-triggered hygiene reminders (store reminder, checkpoint, mandatory stop, hyper-focus, rules-check, periodic-tool), not restatements of rule content (`tracker.py:226-319`). They never carry rule bodies. First-surface rule delivery is the tool result payload from `search`/`read_rule`, a completely separate code path from the nudge block appended after `---`, so suppressing nudges cannot damage retrieval. The first-fire-vs-repeat distinction does not exist in the data model yet and must be built (small).

**Tasks:**
1. Build the first-fire-vs-repeat distinction in `tracker.get_nudge`: track per-session which nudge types have already been delivered, and suppress a repeat of the same type until it is resolved and re-triggered (edge-triggered rather than level-triggered). Roughly 10-20 lines plus interaction with `reset_all` (`tracker.py:187`).
2. Preserve the escalation backstop: a suppressed-but-unresolved nudge still accrues toward `nudge_escalation_threshold`, so genuine non-compliance still hard-stops. Decide and document the exact semantics here (suppression is about not repeating the *reminder text*, not about forgiving the ignore).
3. Gate behind a config flag (`suppress_repeat_nudges`, default on) added in Phase 0, not on any model signal.
4. Use the Phase 0 instrumentation to compare nudge volume and repeat-rate before and after.

**Acceptance:** With the flag on, a session shows each nudge type delivered once per trigger cycle rather than every threshold re-crossing; retrieval payloads are byte-identical; escalation still fires after the configured ignores; flag off restores current behavior exactly; unit tests cover both paths.

---

## Phase 3 — Tool surface: schemas and prescriptive triggers over prose

**Goal:** Replace narrative, changelog, and example-laden tool descriptions with tight, expressive schemas and one-sentence contracts. Crucially, this is not blanket deletion: modern models under-reach for tools by default, so where a tool has a real trigger condition, keep or add a prescriptive "call this when X" one-liner. Remove defensive/aggressive phrasing and internal-doc narrative; keep and sharpen the trigger.

**Reality it corrects / confirms:** 30 tools across 5 files. The codebase uses **zero** `typing.Annotated` + pydantic `Field(description=...)`, and exactly **one** `Literal` (`relations.py:21` `RelationType`, used at `link_knowledge`). So the "tighter schemas" half is genuinely missing, not already done: most param constraints live in docstring prose that FastMCP maps to the free-text `description`, never to per-param schema. Adding `Field`/`Literal` is a new pattern for this repo.

**Tasks:**
1. Inventory (done, see below) and trim the long, example-laden, issue-number/plan-doc narrative docstrings to one-sentence contracts plus a prescriptive trigger where one applies. Highest-value cleanup targets: `search`, `sift_candidates`, `import_rules`, `kb_recall`, `sync_rules`, `restore_rule`, `report_outcome`, `report_error`, `find_*`. Several of these reference internal issues ("issue #64", "MCM2-02", "LODESTONE", "phase 4") that duplicate design docs and commit history.
2. Promote runtime-validated closed sets to `Literal`/enum so the contract reaches the JSON schema. Zero-behavior-risk candidates (runtime already rejects bad values):
   - `pin_item`/`unpin_item`/`get_related`/`promote_to_rule` `entry_type` and `link_knowledge` `source_type`/`target_type` → the `EntityType` set (knowledge/negative/error/rule).
   - `search.scope` → all/knowledge/negative/errors/rules (`_SCOPE_MAP`, `search.py:38-44`), currently a bare `str` with silent fallback.
   - `import_rules.on_duplicate` → update/skip/error.
   - `set_rule_metadata.scope` → `hierarchy.SCOPES`; `.kind` → `hierarchy.KINDS`.
3. Two watch-outs that make "tighten the schema" more than a swap:
   - `set_rule_metadata` uses `importance=-1` and empty-string sentinels to mean "skip this field." A naive `Field(ge=0, le=2)` constraint would break that convention. Either keep the sentinel out of the constrained type, or redesign the skip mechanism first.
   - `import_rules.rules` and `sift_candidates.candidates` are opaque `list[dict]`; the item shape is documented only in prose. The high-value tightening there is a typed item model, which is larger than an enum swap.
4. For `add_knowledge.kind` (finding/decision/insight) and `add_negative.severity` (normal/...): these read as closed sets but have no backing constant and no runtime validation. Decide whether to formalize them into constants + `Literal` (slightly higher risk, needs a call) or leave as free text. Recommend formalizing `kind` at least, since it is a real vocabulary the tool advertises.
5. De-duplicate: where a tool description restates guidance that belongs in the KB or README, cut the restatement and let retrieval carry it. Keep the trigger condition in the tool description itself, where it demonstrably helps.

**Acceptance:** No changelog/issue-number narrative or walkthrough examples remain in tool descriptions; the runtime-validated closed sets are `Literal`/enum; schema changes are additive-only against recent call logs (existing callers already conform); sentinel and `list[dict]` cases handled deliberately, not naively; all tests pass.

---

## Phase 4 — Resident-context slimming

**Goal:** Keep what a fresh session must resident to a minimum, and make the size controllable.

**Reality it corrects (the original premise is largely false):**
- **The handoff is already slim, not a context dump.** `session_handoff` (`tools/session.py:228-277`) stores four agent-authored strings (`status`, `current_task`, `next_steps`, `blockers`) plus a counts-only JSON snapshot. No knowledge content is inlined. Any bulk is free text the previous agent chose to write into those fields.
- **The SessionStart hook is not a long prose blob.** It injects a fixed ~4-sentence directive that reads nothing from the DB and only tells the agent to call `session_start` (`hooks/session_start.py:41-46`, issue #58). The long resume blob comes from the `session_start` tool output and the prior agent's free text, not the hook.
- **No verbosity or budget control exists** anywhere on this path (only a 25-item cap on the invariants list in `session_start`, `:184-192`).

So the real levers are (a) the size and shape of the `session_start` / `get_resume_context` output, and (b) discipline on the free-text handoff fields, not "convert a dump into pointers."

**Tasks:**
1. Add an optional, configurable budget/verbosity control (config flag from Phase 0) on the `session_start` and `get_resume_context` output: a compact default (summary counts, invariants, last handoff fields, pinned pointers) and an opt-in fuller mode. Universal, not model-gated.
2. Prefer typed pointers over inlined content where the resume payload currently inlines: emit knowledge ids, rule ids, and file paths that the next session resolves on demand via existing `search`/`read_rule`/`get_related`. Every emitted pointer must be resolvable by an existing tool (verify in an integration test).
3. Keep always-injecting the top importance tier (the invariants), which is model-agnostic and already implemented.
4. Optional: add light guidance (not enforcement) nudging agents to keep the handoff free-text fields terse, since that is where real bloat originates.

**Acceptance:** Compact mode for a real session lands under the configured budget; every emitted pointer resolves via a tool call in an integration test; the fuller mode and the flag-off path preserve current output; invariants injection unchanged.

---

## Phase 5 — References over restatement (additive)

**Goal:** Where a knowledge entry restates something code or tests already express, store a pointer instead of the prose.

**Reality it corrects:**
- **No `references` field exists** on knowledge entries today (`schema.py:11-27`, `backends/__init__.py:104-120`, `tools/knowledge.py:66-128`). Adding one is genuinely new, additive work.
- **"triplecheck bindings" do not exist.** The string `triplecheck` appears nowhere in the codebase except the original plan document. Phase 5 task 3 ("reuse triplecheck bindings") rests on a nonexistent system; there is no doc-claim-to-code-symbol resolver to reuse. The only existing `references` concept is rule-to-rule wikilinks derived from `[[slug]]` in rule files (`rules_links.py:28,67`), unrelated to the proposed `{type, target, note}` shape.
- **Migration reality is two-backend and asymmetric.** SQLite has a versioned framework (`_mcm_versions` table, not `schema_migrations` as the original named it; `migrate_core` at `schema.py:650-688`, `_has_column`-guarded `ADD COLUMN` is the established additive pattern). Postgres has **no** versioned framework (`adapters/postgres/storage.py:199-200`); migrations are ad-hoc per-issue `ALTER TABLE ADD COLUMN` blocks. A `references` column needs a parallel hand-written block there.
- **Additive is only trivial if the column is not FTS-indexed.** A plain TEXT/JSON column (like `rationale`/`alternatives`, which are excluded from `knowledge_fts`) is trivially additive on SQLite. Making references *searchable* is not: FTS5 vtables cannot be `ALTER`ed and require the drop/recreate/rebuild-triggers dance seen in earlier migrations.

**Tasks:**
1. Add an optional `references` field to the knowledge schema as a nullable JSON/TEXT column: list of `{type: file|symbol|test|url, target, note}`. Additive migration on SQLite via the `_mcm_versions` framework; parallel hand-written `ADD COLUMN` block on Postgres. App-level (de)serialization since it is structured.
2. Thread `references` through `add_knowledge` (optional param) and surface it in search/read results so the consuming agent can open the source of truth instead of trusting prose.
3. Decide searchability explicitly: default to a non-indexed column (trivially additive). Only if references must be FTS-searchable, scope the FTS rebuild as separate, larger work.
4. Drop the original task 3 entirely (no triplecheck to reuse). If populating references from code symbols is desired later, it is a new resolver, filed separately.
5. During the Phase 1 audit, flag knowledge/rule entries whose body substantially duplicates in-repo file content as REWRITE-AS-REFERENCE candidates, feeding this phase.

**Acceptance:** Migration applies cleanly on both an existing SQLite DB and the Postgres pod; at least one real entry converted end to end (prose trimmed, reference added, retrievable, target resolvable); default (non-indexed) path leaves FTS untouched.

---

## Phase 6 — Positioning and docs

**Goal:** Stop describing mcm-engine as a memory system. Native client auto-memory (opencode, Claude Code) now covers personal cross-session memory. mcm's differentiation is shared, correctness-weighted, contradiction-detected team knowledge with provenance and review, embedded in the lodestone platform.

**Tasks:**
1. Rewrite the README framing: lead with shared organizational knowledge and the trust model (correctness weighting, reinforcement, contradiction detection, git-reviewed corpus, provenance/attribution). Remove or replace "memory" language except where technically precise.
2. Add a short "vs. native auto-memory" section: same demand-paged retrieval economics, different eviction and trust policy. Personal memory is table stakes; shared, weighted, reviewed team knowledge is the product.
3. Note the deployment reality (lodestone platform, heterogeneous clients, model-agnostic by design) so future contributors do not reintroduce model-specific assumptions.
4. List any internal Corning-facing docs that pitch mcm as memory (flag, do not edit docs outside the repo).

**Acceptance:** README updated; no unqualified "memory system" claims remain; the model-agnostic stance is documented; flagged external docs listed in the PR description.

---

## Sequencing and dependencies

Phase 0 (instrumentation and flags) comes first, since the ground rule requires measurable, reversible changes and nudge observability does not exist today. Phase 1 (audit, report-only) is independent and high value, start it in parallel with Phase 0. Phase 2 depends on Phase 0's instrumentation and flag surface. Phase 3 is independent. Phase 4 depends on Phase 0's flag surface. Phase 5's schema work can start immediately; its content targets come from Phase 1's flagging. Phase 6 is independent and can go last.

Each phase is its own MR. No phase auto-deletes corpus content; all corpus changes route through the existing git review process. Nothing in this plan detects or branches on the client model.
