# Cutover test plan

Two stages of correctness gates plus an optional Phase C for the daemon path. Designed to be reversible at every step.

## Pre-cutover safety

Before touching anything, snapshot your v1 state:

```bash
V1_DB=$(realpath ~/your/v1/knowledge.db)   # whatever your real path is
V1_RULES=$(realpath ~/your/v1/rules)
cp "$V1_DB" "$V1_DB.pre-mcm2.bak"
tar czf ~/mcm-v1-rules.pre-mcm2.tar.gz -C "$(dirname $V1_RULES)" "$(basename $V1_RULES)"
```

These are the rollback artifacts. Don't proceed without them.

Keep v1 installed and configured throughout. The plan is to add v2 as a SECOND MCP server in `.mcp.json`, not replace v1. After Phase B passes you flip Claude Code to use the v2 one and decommission v1.

## Phase A — standalone embedded, no Docker, no adapters

**Goal:** prove the v1 → v2 dogfood works with zero external infrastructure. This is the "fresh laptop install" guarantee — if Phase A fails, nothing downstream matters.

### A.1 — Install v2 and point it at a copy of your data

```bash
cd /Users/eric/projects/github/unxmaal/mcm2
uv tool install -e .                       # or: pipx install -e .
mkdir -p ~/mcm2-cutover
mcm-engine init --project mcm2-cutover --project-root ~/mcm2-cutover
```

### A.2 — Migrate v1 data into the v2-formatted DB

```bash
mcm-engine migrate \
  --from sqlite://"$V1_DB" \
  --to sqlite://"$HOME/mcm2-cutover/.claude/knowledge.db"
```

**Pass criteria:**
- Migration completes without error.
- Per-table counts in the output match what you expect (eyeball: total ≈ what `sqlite3 $V1_DB "select count(*) from knowledge"` etc. report).
- Spot-check: open the v2 DB and verify a known knowledge row's id is the same as in v1.
  ```bash
  sqlite3 ~/mcm2-cutover/.claude/knowledge.db "SELECT id, topic, summary FROM knowledge ORDER BY id LIMIT 5"
  ```

### A.3 — Copy your rules directory over

```bash
cp -R "$V1_RULES" ~/mcm2-cutover/rules
```

### A.4 — Wire v2 into Claude Code AS A SECOND SERVER

In `.mcp.json` add a new entry alongside (not replacing) your existing one. Name it distinctly:

```json
{
  "mcpServers": {
    "knowledge": { "command": "mcm-engine-v1-binary", "args": [...] },
    "knowledge-v2": {
      "command": "mcm-engine",
      "args": ["run", "--project-root", "/Users/eric/mcm2-cutover"]
    }
  }
}
```

Restart Claude Code. Both servers should connect.

### A.5 — Functional smoke against `knowledge-v2`

Run these tool calls explicitly in a Claude Code session. Each one is a regression gate; if any fails, stop and investigate before continuing.

| # | Call | Pass criteria |
|---|------|---------------|
| 1 | `mcp__knowledge-v2__session_start` | Returns counts > 0 across knowledge/negative/errors/rules. Last handoff present (from migrated session row). No exceptions in the server log. |
| 2 | `mcp__knowledge-v2__search query="<a topic you know exists>"` | Returns results. Ranking looks plausible. No `[STALE]` on recent items. |
| 3 | `mcp__knowledge-v2__search query="zzqqxxnotanything"` | Returns "No results for ..." cleanly, no stack trace. |
| 4 | `mcp__knowledge-v2__add_knowledge topic="cutover smoke A5" summary="testing v2" kind="finding"` | Stores. `search query="cutover smoke A5"` returns it. |
| 5 | `mcp__knowledge-v2__add_negative category="cutover" what_failed="dummy"` | Stores. Reachable via `search scope="negative" query="cutover"`. |
| 6 | `mcp__knowledge-v2__report_error error_text="cutover smoke error"` | Logs the error AND returns auto-search results. Both halves present. |
| 7 | `mcp__knowledge-v2__add_rule title="Cutover smoke A5" keywords="cutover,test"` | Creates `rules/cutover-smoke-a5.md` on disk. Row exists with file_path set. |
| 8 | `mcp__knowledge-v2__read_rule file_path="rules/cutover-smoke-a5.md"` | Returns the file's content. `hit_count` increments. |
| 9 | `mcp__knowledge-v2__reinforce_knowledge` on the entry from #4 | Reinforcement count goes up. `search` shows the row ranked higher next time. |
| 10 | `mcp__knowledge-v2__pin_item entry_type="knowledge" entry_id=<#4>` then search | Returns with `[PINNED]` tag. |
| 11 | `mcp__knowledge-v2__sync_rules` | Runs without error. Returns counts. Any orphans get archived (soft-delete), not hard-deleted. |
| 12 | `mcp__knowledge-v2__session_handoff status="cutover phase A complete" current_task="..."` | Returns "Session handoff recorded. Counters reset." Row visible in `sqlite3 $V2_DB "SELECT id, status FROM sessions ORDER BY id DESC LIMIT 1"`. |

### A.6 — Restart-and-resume

```bash
# Quit Claude Code, restart. Then in the new session:
```

| Call | Pass criteria |
|------|---------------|
| `session_start` | Last handoff line shows the status from A.5#12. |
| `get_resume_context` | Returns the snapshot saved at handoff. |

### A.7 — Verify v1 is untouched

```bash
sqlite3 "$V1_DB" "SELECT COUNT(*) FROM knowledge"   # same count as before A
```

A.7 must be unchanged from your pre-cutover number. If it isn't, something is writing back to the v1 DB and you should stop.

**Phase A acceptance:** all 12 functional smokes pass + restart-and-resume works + v1 untouched. If this passes, mcm2 embedded is a drop-in for v1.

## Phase B — with the adapter stack (Docker compose)

**Goal:** prove the orthogonal-axis switches actually work in a production-like config. This validates that the abstractions you built held up.

### B.1 — Bring up the stack

```bash
cd /Users/eric/projects/github/unxmaal/mcm2
docker compose -f tests/docker-compose.yml up -d postgres redis opensearch
# Wait for healthchecks:
docker compose -f tests/docker-compose.yml ps
```

### B.2 — Migrate the Phase A v2 SQLite DB to Postgres

```bash
mcm-engine migrate \
  --from sqlite://"$HOME/mcm2-cutover/.claude/knowledge.db" \
  --to "postgresql://mcm:mcm@127.0.0.1:55432/mcm_test"
```

Pass criteria: total counts match Phase A's migrated DB; a spot-check shows the same row ids.

### B.3 — Write a `mcm-engine.yaml` that uses every axis

```yaml
# ~/mcm2-cutover/mcm-engine.yaml
project_name: mcm2-cutover
rules_path: rules/
nudges:
  store_reminder_turns: 10
  checkpoint_turns: 25
  mandatory_stop_turns: 50
backends:
  storage: postgres
  counters: redis
  search: opensearch
  session: embedded
  storage_options:
    dsn: postgresql://mcm:mcm@127.0.0.1:55432/mcm_test
  counters_options:
    url: redis://127.0.0.1:56379/0
    namespace: "mcm:cutover:"
  search_options:
    url: http://127.0.0.1:59200
    index_prefix: "mcm-cutover-"
    # OpenSearch adapter needs a storage handle for sync; for stdio mode
    # we'll rely on the brute-force re-index per search. For serve mode,
    # the watcher cascade handles it.
```

### B.4 — Re-run the same A.5 functional smoke matrix against `knowledge-v2`

Restart Claude Code so the new config takes effect. Run the same 12 calls. Same pass criteria. Additionally:

- After call #4 (add_knowledge), verify the row is in **Postgres**, not SQLite:
  ```bash
  docker exec mcm2-test-postgres psql -U mcm -d mcm_test -c \
    "SELECT id, topic FROM knowledge WHERE topic = 'cutover smoke A5'"
  ```
- After call #9 (reinforce_knowledge), verify the counter is in **Redis**, not the Postgres row's counter columns:
  ```bash
  docker exec mcm2-test-redis redis-cli ZSCORE mcm:cutover:counters:knowledge:reinforcement_count <id>
  ```
- After call #2 (search), verify OpenSearch was actually hit:
  ```bash
  curl -s "http://127.0.0.1:59200/mcm-cutover-knowledge/_count?q=<term>"
  ```

If all three axes show evidence of being hit, the orthogonal wiring is real, not just configured.

### B.5 — Containers down → engine fails honestly

```bash
docker compose -f tests/docker-compose.yml stop postgres
```

Restart Claude Code with the same config. `session_start` should fail with a clear "can't reach Postgres at ..." error, not a stack trace or silent fallback. This is the test that proves the engine doesn't paper over connection failures.

Then bring postgres back up and verify recovery:
```bash
docker compose -f tests/docker-compose.yml start postgres
# Restart Claude Code; session_start should succeed.
```

**Phase B acceptance:** A.5 matrix passes against the adapter mix + per-axis evidence shows each backend was actually used + failure mode is honest.

## Phase C — daemon mode (optional, but it's the whole point of the rewrite)

**Goal:** validate the long-lived `mcm-engine serve` + watcher cascade + HTTP transport.

### C.1 — Start the daemon

```bash
mcm-engine serve --project-root ~/mcm2-cutover --host 127.0.0.1 --port 8080
```

In another shell:
```bash
curl -sf http://127.0.0.1:8080/healthz       # → {"status":"ok"}
curl -sf http://127.0.0.1:8080/readyz        # → all three backends "ok"
```

### C.2 — Watcher cascade live test

While the daemon is running, in another shell:
```bash
# Create a new rule file directly on disk (bypassing add_rule).
cat > ~/mcm2-cutover/rules/watcher-test.md <<'EOF'
# Watcher cascade live test
**Keywords:** watcher, test

Body content for the live watcher test.
EOF
sleep 2
```

Then in Claude Code:
- `mcp__knowledge-v2__search query="watcher cascade live"` → should find it.

Now edit the file:
```bash
sed -i '' 's/Body content/Updated body/' ~/mcm2-cutover/rules/watcher-test.md
sleep 2
```

Then in Claude Code:
- `mcp__knowledge-v2__read_rule file_path="rules/watcher-test.md"` → returns the updated content.
- `mcp__knowledge-v2__search query="updated body"` → finds it.

Now delete:
```bash
rm ~/mcm2-cutover/rules/watcher-test.md
sleep 2
```

- The rule should be archived (soft-deleted), not hard-deleted. Verify:
  ```bash
  docker exec mcm2-test-postgres psql -U mcm -d mcm_test -c \
    "SELECT id, title, archived FROM rules WHERE title='Watcher cascade live test'"
  ```
  → archived = `t`.

### C.3 — Connect Claude Code via HTTP

This is the real production deployment shape. Update `.mcp.json` for the v2 server to use HTTP transport (Claude Code 1.x supports SSE; check your version's docs). If your version doesn't, skip C.3 — daemon mode for a different MCP client.

## Cutover decision

After Phase A passes: **v2 embedded is a drop-in for v1.** You can flip `.mcp.json` to make `knowledge-v2` the only knowledge server, rename it back to `knowledge`, and uninstall v1.

After Phase B passes: **adapters work.** You can switch to the Postgres+Redis+OpenSearch config full-time if there's a reason to (heavy use, multiple agents, etc.). Most users probably stay on embedded.

After Phase C passes: **the daemon path is live.** Worth doing only if you actually want to run mcm-engine as a long-running service rather than spawned per Claude Code session.

## Rollback

At any point during Phases A/B:
1. Edit `.mcp.json` to remove the `knowledge-v2` entry. Restart Claude Code. v1 is still serving as it was before — nothing was touched.
2. If you flipped fully to v2 and need to back out: restore `$V1_DB.pre-mcm2.bak` to `$V1_DB`, put v1's entry back in `.mcp.json`.

The v1 DB is never written to during the cutover (verified in A.7). Rollback is a config flip, not a data restore.

## What this plan does NOT cover

- Multi-project setups. If you use mcm-engine in more than one project, repeat A for each. The migration is per-DB.
- Plugin compatibility. Per the plan there are no third-party plugins in tree; if you have any locally, the MCM2-07 contract change (SearchScope drops `.search()`) may break them — covered by the rule `rules/mcm2/plugin-searchscope-is-a-passive-descriptor-sql-lives-in-searchbackendsearch-plugin.md`.
- Performance characterization. The new stack has more layers; if you notice latency, look at it then.
- Concurrent multi-client access. The daemon path supports it; the tests don't actually exercise N simultaneous clients.
