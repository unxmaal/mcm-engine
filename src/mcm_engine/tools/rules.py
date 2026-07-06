"""External rules file tools — add_rule, read_rule, promote_to_rule,
sync_rules, reinforce_rule.

Rewired in MCM2-02 (Phase 0): all SQL routes through SqliteStorage /
SqliteCounters. Orphan removal in sync_rules now soft-deletes (sets
archived=1) instead of hard-deleting, matching the watcher-cascade
direction (MCM2-23) and the v7 schema columns.
"""
from __future__ import annotations

import os
import re
import time
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..backends import EntityType, RuleRow
from ..db import log
from ..destructive import archive_would_storm
from ..files.watcher import compute_content_hash
from ..principal import resolve_actor
from ..rules_links import build_wikilink_relations, extract_wikilinks
from ..sanitize import scan_injection, wrap_untrusted
from ..tracker import SessionTracker
from ..wiring import Context, coerce_context

__all__ = ["extract_wikilinks", "register_rules_tools"]


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    text = text.strip("-")
    return text or "untitled"


def _parse_rule_file(path: Path) -> dict:
    """Extract metadata from a rule markdown file.

    Always populates ``content_hash`` so callers (notably sync_rules) can
    seed it on the row without re-reading the file.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}

    # `content` holds the full markdown body (issue #10) — distinct from
    # `description`, which stays the leading-paragraph FTS signal below.
    result: dict[str, str] = {
        "content_hash": compute_content_hash(content),
        "content": content,
    }
    lines = content.split("\n")

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            result["title"] = stripped[2:].strip()
            break

    for line in lines:
        stripped = line.strip()
        kw_match = re.match(r"\*\*Keywords?:\*\*\s*(.+)", stripped, re.IGNORECASE)
        if kw_match:
            result["keywords"] = kw_match.group(1).strip()
            continue
        cat_match = re.match(r"\*\*Category:\*\*\s*(.+)", stripped, re.IGNORECASE)
        if cat_match:
            result["category"] = cat_match.group(1).strip()
            continue

    in_body = False
    desc_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not in_body:
            if stripped.startswith("# ") and not stripped.startswith("## "):
                continue
            if re.match(r"\*\*(Keywords?|Category):\*\*", stripped, re.IGNORECASE):
                continue
            if stripped == "":
                continue
            in_body = True
        if in_body:
            if stripped == "" and desc_lines:
                break
            if stripped.startswith("## "):
                break
            desc_lines.append(stripped)

    if desc_lines:
        result["description"] = " ".join(desc_lines)

    return result


def _generate_rule_content(title: str, keywords: str, category: str, content: str) -> str:
    parts = [f"# {title}", ""]
    parts.append(f"**Keywords:** {keywords}")
    if category:
        parts.append(f"**Category:** {category}")
    parts.append("")
    parts.append(content)
    parts.append("")
    return "\n".join(parts)


def _with_nudge(result: str, tracker: SessionTracker, topic: str | None = None) -> str:
    nudge = tracker.get_nudge(topic)
    if nudge:
        return f"{result}\n\n---\n{nudge}"
    return result


# --- import_rules (bulk, pod-native) helpers -----------------------------


class _BatchAbort(Exception):
    """Raised inside the import transaction to force a rollback while carrying
    the per-rule status back to the caller (the on_duplicate=error path)."""

    def __init__(self, message: str, results: list[dict]):
        super().__init__(message)
        self.results = results


def _batch_reject(message: str, rules: object) -> dict:
    """A pre-DB validation failure: reject the whole batch, write nothing."""
    n = len(rules) if isinstance(rules, list) else 0
    return {"error": message, "total": n, "created": 0, "updated": 0,
            "skipped": 0, "errors": 0, "rules": []}


def _tally(results: list[dict]) -> dict:
    counts = {"created": 0, "updated": 0, "skipped": 0, "errors": 0}
    for r in results:
        status = r.get("status")
        if status == "error":
            counts["errors"] += 1
        elif status in counts:
            counts[status] += 1
    return {"total": len(results), **counts}


def _apply_import_batch(
    storage,
    items: list[dict],
    who: str,
    on_duplicate: str,
    src_repo: str,
    src_ref: str,
    src_commit: str,
) -> list[dict]:
    """Per-row create/update/skip against title collisions, mirroring
    add_rule's semantics. Runs inside storage.transaction() so the whole
    batch (rows AND their events) commits or rolls back as one unit.

    Dedup is read-then-write via find_rule_by_title — there is no UNIQUE
    constraint on rules.title to hang an ON CONFLICT on (see issue #14).
    """
    # error mode: verify no collision BEFORE any write, so an abort leaves the
    # DB untouched and reports honest per-rule status.
    if on_duplicate == "error":
        collisions = {
            it["title"] for it in items
            if storage.find_rule_by_title(it["title"]) is not None
        }
        if collisions:
            results = [
                {"title": it["title"], "status": "error",
                 "error": "rule already exists"}
                if it["title"] in collisions else
                {"title": it["title"], "status": "skipped",
                 "error": "batch aborted: duplicate title(s) present"}
                for it in items
            ]
            raise _BatchAbort(
                f"on_duplicate=error: {len(collisions)} title(s) already exist: "
                f"{', '.join(sorted(collisions))}",
                results,
            )

    results: list[dict] = []
    for it in items:
        title = it["title"]
        content = it["content"]
        content_hash = compute_content_hash(content)
        existing = storage.find_rule_by_title(title)

        if existing is not None:
            if on_duplicate == "skip":
                results.append({"title": title, "status": "skipped",
                                "rule_id": existing.id})
                continue
            # on_duplicate == "update" (error mode already aborted above)
            material = content != (existing.content or "")
            storage.update_rule(
                existing.id,
                keywords=it["keywords"],
                description=content[:500],
                category=it["category"] or existing.category,
                file_path=it["file_path"] or existing.file_path,
                content=content,
                content_hash=content_hash,
                updated_by=who,
            )
            if material:
                storage.insert_rule_event(
                    existing.id, "updated", who,
                    content_hash=content_hash,
                    source_repo=src_repo or None,
                    source_ref=src_ref or None,
                    source_commit=src_commit or None,
                )
                results.append({"title": title, "status": "updated",
                                "rule_id": existing.id})
            else:
                # Identical re-import: row rewritten with same body, no event.
                results.append({"title": title, "status": "skipped",
                                "rule_id": existing.id})
            continue

        rule_id = storage.insert_rule(RuleRow(
            id=0,
            title=title,
            keywords=it["keywords"],
            file_path=it["file_path"] or None,
            description=content[:500],
            category=it["category"] or None,
            content_hash=content_hash,
            content=content,
            created_by=who,
            updated_by=who,
        ))
        storage.insert_rule_event(
            rule_id, "created", who,
            content_hash=content_hash,
            source_repo=src_repo or None,
            source_ref=src_ref or None,
            source_commit=src_commit or None,
        )
        results.append({"title": title, "status": "created", "rule_id": rule_id})

    return results


_DEFAULT_SIFT_MAX_SPANS = 25


def _sift_max_spans() -> int:
    """Per-call span ceiling for sift_candidates (issue #76). Env-tunable via
    MCM_SIFT_MAX_SPANS; falls back to the default on a missing/invalid value."""
    try:
        v = int(os.environ.get("MCM_SIFT_MAX_SPANS", "") or _DEFAULT_SIFT_MAX_SPANS)
        return v if v > 0 else _DEFAULT_SIFT_MAX_SPANS
    except ValueError:
        return _DEFAULT_SIFT_MAX_SPANS


def _active_conflict_items(storage):
    """(items, titles, importances) over ACTIVE rules for conflict detection
    (issue #32). items = [(id, topic=title+keywords, body=content)];
    importances = {id: importance} for the #64 tiebreak."""
    items = []
    titles = {}
    importances = {}
    for row in storage.iter_entries(EntityType.RULE):
        if getattr(row, "archived", False):
            continue
        if getattr(row, "status", "active") == "superseded":
            continue
        titles[row.id] = row.title
        importances[row.id] = getattr(row, "importance", 0) or 0
        items.append((row.id, f"{row.title} {row.keywords or ''}", row.content or ""))
    return items, titles, importances


def _conflict_note_for(storage, new_id):
    """Non-blocking add_rule note when the just-added rule conflicts with an
    existing active rule (issue #32) — topically similar but body-divergent.
    Surfacing only: never supersedes, no LLM."""
    from ..dedup import find_conflicts

    items, titles, _importances = _active_conflict_items(storage)
    pairs = find_conflicts(items)
    conflicting: dict = {}
    for a, b, label in pairs:
        if new_id == a:
            conflicting[b] = label
        elif new_id == b:
            conflicting[a] = label
    if not conflicting:
        return ""
    parts = "; ".join(
        f"#{cid} '{titles.get(cid, '')}' ({conflicting[cid]})"
        for cid in sorted(conflicting)
    )
    return f"\n  ⚠ may conflict with {parts} — consider supersede_rule"


def register_rules_tools(
    mcp: FastMCP,
    ctx_or_db,
    tracker: SessionTracker,
    project_name: str,
    rules_paths: list[Path],
    project_root: Path,
    files_authoritative: bool = True,
) -> None:
    """Register add_rule, read_rule, promote_to_rule, sync_rules,
    reinforce_rule, import_rules, restore_rule tools.

    Accepts a Context or a raw KnowledgeDB for backward compat.

    ``files_authoritative`` (issue #16) reflects config.source_of_truth: when
    False (database mode) the DB is authoritative, so add_rule writes no
    markdown file and read_rule prefers the stored body over any local file.
    Defaults True so existing callers (and every test) keep files-mode behavior.
    """
    ctx = coerce_context(ctx_or_db)
    primary_rules_path = rules_paths[0] if rules_paths else project_root / "rules"
    storage = ctx.storage
    counters = ctx.counters

    @mcp.tool()
    def add_rule(
        title: str,
        keywords: str,
        content: str = "",
        category: str = "",
        file_path: str = "",
        actor: str = "",
        source_repo: str = "",
        source_ref: str = "",
        source_commit: str = "",
    ) -> str:
        """Create or index a rule file. `actor` (falling back to MCM_ACTOR,
        then the transport principal, then 'nobody') is recorded as the
        author on the row and in the rule_events audit log (issue #10)."""
        tracker.record_call("add_rule", topic=title)
        tracker.record_store()
        who = resolve_actor(actor)

        existing = storage.find_rule_by_title(title)
        if existing is not None:
            fields: dict = {
                "keywords": keywords,
                "description": (content[:500] if content else ""),
                "category": category,
                "file_path": file_path or existing.file_path,
                "updated_by": who,
            }
            # Only touch the full body when the caller supplied one, so a
            # keyword-only re-index doesn't wipe stored content. A material
            # change (new body != stored body) emits an `updated` event;
            # re-adding identical content is an idempotent no-op event-wise.
            material = bool(content) and content != (existing.content or "")
            if content:
                fields["content"] = content
                fields["content_hash"] = compute_content_hash(content)
            storage.update_rule(existing.id, **fields)
            if material:
                storage.insert_rule_event(
                    existing.id, "updated", who,
                    content_hash=fields.get("content_hash"),
                    source_repo=source_repo or None,
                    source_ref=source_ref or None,
                    source_commit=source_commit or None,
                )
            return _with_nudge(
                f"Updated existing rule: {title} (id={existing.id})",
                tracker, title,
            )

        actual_path: str = file_path
        warning = ""
        # content_hash is needed by the watcher cascade so engine-initiated
        # writes don't trip a redundant re-cascade — see
        # docs/watcher-cascade.md and rules/mcm2/.
        content_hash: str | None = None

        if file_path:
            full = project_root / file_path
            if full.exists():
                parsed = _parse_rule_file(full)
                if not content and parsed.get("description"):
                    content = parsed["description"]
                content_hash = compute_content_hash(
                    full.read_text(encoding="utf-8")
                )
            else:
                warning = f"\nWarning: file '{file_path}' does not exist. Rule indexed without file backing."
        elif files_authoritative:
            cat_dir = primary_rules_path / category if category else primary_rules_path
            cat_dir.mkdir(parents=True, exist_ok=True)
            slug = _slugify(title)
            new_file = cat_dir / f"{slug}.md"

            counter = 1
            while new_file.exists():
                new_file = cat_dir / f"{slug}-{counter}.md"
                counter += 1

            file_content = _generate_rule_content(title, keywords, category, content or "")
            new_file.write_text(file_content, encoding="utf-8")
            actual_path = str(new_file.relative_to(project_root))
            content_hash = compute_content_hash(file_content)
        else:
            # database mode (issue #16): the DB is authoritative and there is
            # no filesystem to own. Store the body in the row only; write no
            # markdown file and leave file_path empty so the watcher (if it
            # ever runs) never treats this as a managed, then-missing file.
            content_hash = compute_content_hash(content) if content else None

        description = content[:500] if content else ""
        rule_id = storage.insert_rule(RuleRow(
            id=0,
            title=title,
            keywords=keywords,
            file_path=actual_path or None,
            description=description or None,
            category=category or None,
            content_hash=content_hash,
            content=content or None,
            created_by=who,
            updated_by=who,
        ))
        storage.insert_rule_event(
            rule_id, "created", who,
            content_hash=content_hash,
            source_repo=source_repo or None,
            source_ref=source_ref or None,
            source_commit=source_commit or None,
        )

        conflict_note = _conflict_note_for(storage, rule_id)

        msg = f"Rule added: {title}"
        if actual_path:
            msg += f"\n  File: {actual_path}"
        if warning:
            msg += warning
        if conflict_note:
            msg += conflict_note
        markers = scan_injection(f"{title}\n{content}")
        if markers:
            msg += (f"\n  ⚠ possible injection markers ({', '.join(markers)}) "
                    "— stored but flagged for review")
        try:  # #37: storing a rule cost tokens.
            storage.record_token_event("spent", max(1, len(content or "") // 4))
        except Exception:
            pass
        return _with_nudge(msg, tracker, title)

    @mcp.tool()
    def import_rules(
        rules: list[dict],
        actor: str = "",
        source_repo: str = "",
        source_ref: str = "",
        source_commit: str = "",
        on_duplicate: str = "update",
    ) -> dict:
        """Bulk-import rules in a single call, for filesystem-less deploys
        (a pod that cannot see the rules/ tree). Each rule carries its full
        `content` in the payload; nothing is read from or written to disk.

        Unlike calling `add_rule` in a loop, this counts as ONE tracked call,
        so a documented batch load does not trip the look-first nudge. `actor`,
        `source_repo`, `source_ref`, `source_commit` are shared across every
        rule in the batch and recorded on each rule_events row.

        Args:
            rules: list of {title, keywords, content, category?, file_path?}
                dicts. title, keywords, content are required and non-empty.
                file_path is provenance-only here (no file is written).
            on_duplicate: behavior when a title already exists —
                "update" (default): overwrite; emit an `updated` event only
                    when the body actually changed (identical re-import is a
                    no-op, reported as skipped).
                "skip": leave the existing row untouched, emit no event.
                "error": abort the whole batch if ANY title already exists;
                    nothing is written.

        Returns a dict {total, created, updated, skipped, errors, rules:[...]}
        with per-rule {title, status, rule_id?/error?}. The whole batch is one
        transaction: on a mid-batch failure nothing is written. A validation
        failure (missing field, duplicate title within the batch, bad
        on_duplicate) rejects the batch with a top-level `error`.
        """
        tracker.record_call("import_rules", topic=f"{len(rules)} rules")
        tracker.record_store()

        if on_duplicate not in ("update", "skip", "error"):
            return _batch_reject(
                f"invalid on_duplicate {on_duplicate!r}; "
                "expected update|skip|error",
                rules,
            )
        if not rules:
            return {"total": 0, "created": 0, "updated": 0,
                    "skipped": 0, "errors": 0, "rules": []}

        # Validation pass — nothing touches the DB until this clears.
        items: list[dict] = []
        seen: set[str] = set()
        for i, r in enumerate(rules):
            if not isinstance(r, dict):
                return _batch_reject(f"rule at index {i} is not an object", rules)
            title = (r.get("title") or "").strip()
            keywords = (r.get("keywords") or "").strip()
            content = r.get("content") or ""
            if not title:
                return _batch_reject(f"rule at index {i} missing title", rules)
            if not keywords:
                return _batch_reject(f"rule {title!r} missing keywords", rules)
            if not content:
                return _batch_reject(f"rule {title!r} missing content", rules)
            if title in seen:
                return _batch_reject(
                    f"duplicate title within batch: {title!r}", rules)
            seen.add(title)
            items.append({
                "title": title,
                "keywords": keywords,
                "content": content,
                "category": (r.get("category") or "").strip(),
                "file_path": (r.get("file_path") or "").strip(),
            })

        who = resolve_actor(actor)

        # One atomic transaction spanning every row AND its events.
        try:
            with storage.transaction():
                results = _apply_import_batch(
                    storage, items, who, on_duplicate,
                    source_repo, source_ref, source_commit,
                )
        except _BatchAbort as abort:
            # on_duplicate=error with collisions — nothing was written.
            return {"error": str(abort), **_tally(abort.results),
                    "rules": abort.results}
        except Exception as e:  # defensive: transaction already rolled back
            log(f"import_rules rolled back on error: {e}")
            return {
                "error": f"import failed, rolled back: {e}",
                "total": len(items), "created": 0, "updated": 0,
                "skipped": 0, "errors": len(items),
                "rules": [{"title": it["title"], "status": "error",
                           "error": str(e)} for it in items],
            }

        return {**_tally(results), "rules": results}

    @mcp.tool()
    def restore_rule(
        rule_ids: list[int] | None = None,
        all_archived: bool = False,
        actor: str = "",
    ) -> dict:
        """Un-archive soft-deleted rules (issue #16 recovery tool).

        Archived rules are invisible to search but not deleted. Use this to
        recover after an accidental orphan sweep (e.g. a watcher archive-storm
        in a mis-deployed pod) or any soft-delete. Provide explicit `rule_ids`,
        or `all_archived=True` to restore every archived rule at once. Emits a
        `restored` event per rule, attributed to the resolved actor.

        Returns {restored: <count>, rule_ids: [<restored ids>]}.
        """
        tracker.record_call("restore_rule")
        who = resolve_actor(actor)

        targets: list[int] = []
        if all_archived:
            targets.extend(r.id for r in storage.list_archived_rules())
        if rule_ids:
            targets.extend(rule_ids)
        targets = sorted(set(targets))

        restored: list[int] = []
        with storage.transaction():
            for rid in targets:
                row = storage.find_by_id(EntityType.RULE, rid)
                if row is None or not row.archived:
                    continue
                storage.restore_rule(rid)
                storage.insert_rule_event(rid, "restored", who)
                restored.append(rid)
        return {"restored": len(restored), "rule_ids": restored}

    @mcp.tool()
    def read_rule(file_path: str) -> str:
        """Read a rule's contents. Prefers the file on disk; when the file
        is absent (e.g. a pod deployment with no filesystem for rules loaded
        via add_rule), falls back to the full body stored in rules.content
        (issue #10). Increments hit_count for tracking in either case."""
        tracker.record_call("read_rule", topic=file_path)

        fp = Path(file_path)
        full = fp if fp.is_absolute() else project_root / file_path
        row = storage.find_rule_by_file_path(file_path)

        def _serve_body(body: str) -> str:
            # #34: delimit stored content as untrusted DATA at read time so a
            # downstream agent reads a rule as a past finding, not live
            # instructions (enforced here in retrieval code, not the prompt).
            try:  # #37: reading a stored rule saved re-deriving it.
                storage.record_token_event("saved", max(1, len(body) // 4))
            except Exception:
                pass
            return _with_nudge(wrap_untrusted(body), tracker, file_path)

        # database mode (issue #16): the DB is authoritative — prefer the stored
        # body over any (stale) local file. Falls through to disk only if the
        # row has no content. files mode keeps the disk-first order below.
        if not files_authoritative and row is not None and row.content:
            counters.increment(EntityType.RULE, row.id, "hit_count")
            counters.increment(EntityType.RULE, row.id, "last_hit_at")
            return _serve_body(row.content)

        if full.exists():
            try:
                content = full.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                return _with_nudge(f"Error reading {file_path}: {e}", tracker)
            if row is not None:
                counters.increment(EntityType.RULE, row.id, "hit_count")
                counters.increment(EntityType.RULE, row.id, "last_hit_at")
            return _serve_body(content)

        # No file on disk — serve the DB copy of the body if we have one.
        if row is not None and row.content:
            counters.increment(EntityType.RULE, row.id, "hit_count")
            counters.increment(EntityType.RULE, row.id, "last_hit_at")
            return _serve_body(row.content)

        return _with_nudge(f"Rule file not found: {file_path}", tracker)

    @mcp.tool()
    def list_rules(
        include_archived: bool = False, min_importance: int = 0, limit: int = 100,
    ) -> str:
        """List rules with their hierarchy axes (importance/scope/kind) and the
        derived signals (hits, reinforcement, correctness). Ordered
        importance-first so the highest-binding rules — and any tier inflation —
        surface at the top (issue #64). `min_importance` filters to a tier and
        up (2 = invariants only); `limit` caps the output."""
        tracker.record_call("list_rules")
        rows = storage.list_rules(
            include_archived=include_archived,
            min_importance=min_importance,
            limit=limit or None,
        )
        if not rows:
            return _with_nudge("(no rules)", tracker)
        lines = [
            "# rules — id | importance | scope | kind | category | hits | "
            "reinf | correct/incorrect | status | title"
        ]
        for r in rows:
            lines.append(
                f"#{r.id} | imp={r.importance} | {r.scope} | {r.kind} | "
                f"{r.category or '-'} | h={r.hit_count} | rf={r.reinforcement_count} | "
                f"{r.correct_count}/{r.incorrect_count} | {r.status} | {r.title}"
            )
        return _with_nudge("\n".join(lines), tracker)

    @mcp.tool()
    def set_rule_metadata(
        rule_id: int, importance: int = -1, scope: str = "", kind: str = "",
        category: str = "", actor: str = "",
    ) -> str:
        """Tune a rule's hierarchy axes (issue #64): importance (0..2, where
        2=invariant), scope (universal|conditional), kind (directive|fact),
        category (free text). Only the arguments you pass are changed — leave
        importance at -1 and the string fields empty to skip them. Emits an
        audited 'metadata' event. `actor` falls back to MCM_ACTOR / the
        transport principal / 'nobody'."""
        tracker.record_call("set_rule_metadata", topic=str(rule_id))
        tracker.record_store()
        who = resolve_actor(actor)
        try:
            updated = storage.set_rule_metadata(
                rule_id,
                importance=(None if importance < 0 else importance),
                scope=(scope or None),
                kind=(kind or None),
                category=(category or None),
                actor=who,
            )
        except ValueError as e:
            return _with_nudge(f"set_rule_metadata rejected: {e}", tracker)
        if updated is None:
            return _with_nudge(f"rule not found: #{rule_id}", tracker)
        return _with_nudge(
            f"rule #{rule_id} updated: importance={updated.importance}, "
            f"scope={updated.scope}, kind={updated.kind}, "
            f"category={updated.category or '-'}",
            tracker,
        )

    @mcp.tool()
    def promote_to_rule(
        source_type: str,
        source_id: int,
        title: str,
        category: str = "",
        keywords: str = "",
        actor: str = "",
    ) -> str:
        """Promote a DB entry to a persistent rule file."""
        tracker.record_call("promote_to_rule", topic=title)
        who = resolve_actor(actor)

        try:
            etype = EntityType(source_type)
        except ValueError:
            return _with_nudge(
                f"Invalid source_type '{source_type}'. Use 'knowledge', 'negative', or 'error'.",
                tracker,
            )

        row = storage.find_by_id(etype, source_id)
        if row is None:
            label = source_type.capitalize() if source_type != "negative" else "Negative knowledge"
            return _with_nudge(f"{label} entry {source_id} not found.", tracker)

        if etype is EntityType.KNOWLEDGE:
            content = row.summary
            if row.detail:
                content += f"\n\n{row.detail}"
            if not keywords:
                keywords = row.tags or row.topic
        elif etype is EntityType.NEGATIVE:
            content = f"**What failed:** {row.what_failed}"
            if row.why_failed:
                content += f"\n\n**Why:** {row.why_failed}"
            if row.correct_approach:
                content += f"\n\n## Fix\n\n{row.correct_approach}"
            if not keywords:
                keywords = row.category
            if not category:
                category = row.category
        elif etype is EntityType.ERROR:
            content = f"**Error:** {row.pattern}"
            if row.context:
                content += f"\n\n**Context:** {row.context}"
            if row.root_cause:
                content += f"\n\n**Root cause:** {row.root_cause}"
            if row.fix:
                content += f"\n\n## Fix\n\n{row.fix}"
            if not keywords:
                keywords = row.pattern[:100]
        else:
            return _with_nudge(
                f"Cannot promote source_type '{source_type}' to a rule.", tracker,
            )

        result = add_rule(
            title=title,
            keywords=keywords,
            content=content,
            category=category,
            actor=who,
        )
        # add_rule already emitted `created`; add the `promoted` event so
        # the audit trail records the DB origin.
        promoted = storage.find_rule_by_title(title)
        if promoted is not None:
            storage.insert_rule_event(
                promoted.id, "promoted", who,
                note=f"{source_type}:{source_id}",
            )
        return result

    @mcp.tool()
    def sync_rules(
        actor: str = "",
        source_repo: str = "",
        source_ref: str = "",
        source_commit: str = "",
        force: bool = False,
    ) -> str:
        """Re-index all .md files. Upserts DB entries; archives orphans
        (soft-delete) for files that no longer exist. Every state change
        emits a rule_events row attributed to `actor` (issue #10), with
        source_repo/ref/commit propagated to each event.

        Blast-radius guard (issue #20): if the sweep would archive a large
        fraction of the corpus (a wrong `project_root`, empty/misrooted rules
        dir), it refuses and archives NOTHING — pass `force=True` to override.
        This is the same guard the watcher cascade uses."""
        tracker.record_call("sync_rules")
        who = resolve_actor(actor)
        src_repo = source_repo or None
        src_ref = source_ref or None
        src_commit = source_commit or None

        md_files: list[Path] = []
        missing_paths: list[str] = []
        for rp in rules_paths:
            if rp.exists():
                md_files.extend(sorted(rp.rglob("*.md")))
            else:
                missing_paths.append(str(rp))

        if not md_files and missing_paths:
            return _with_nudge(
                f"No rules directories found: {', '.join(missing_paths)}", tracker,
            )

        indexed = 0
        updated = 0
        archived = 0

        for md_file in md_files:
            try:
                rel_path = str(md_file.relative_to(project_root))
            except ValueError:
                rel_path = str(md_file)
            parsed = _parse_rule_file(md_file)
            if not parsed.get("title"):
                continue

            title = parsed["title"]
            keywords = parsed.get("keywords", "")
            category = parsed.get("category", "")
            description = parsed.get("description", "")
            content = parsed.get("content")
            content_hash = parsed.get("content_hash")

            existing = storage.find_rule_by_file_path(rel_path)
            if existing is not None:
                storage.update_rule(
                    existing.id,
                    title=title,
                    keywords=keywords,
                    description=description,
                    category=category,
                    content_hash=content_hash,
                    content=content,
                    updated_by=who,
                )
                # Files-win: a reappeared file un-archives its row.
                if existing.archived:
                    storage.restore_rule(existing.id)
                    storage.insert_rule_event(
                        existing.id, "restored", who,
                        content_hash=content_hash, source_repo=src_repo,
                        source_ref=src_ref, source_commit=src_commit,
                    )
                # A changed body (content_hash differs from the stored one)
                # is a material update worth an event; an unchanged re-sync
                # is not.
                if content_hash and content_hash != existing.content_hash:
                    storage.insert_rule_event(
                        existing.id, "updated", who,
                        content_hash=content_hash, source_repo=src_repo,
                        source_ref=src_ref, source_commit=src_commit,
                    )
                updated += 1
            else:
                rid = storage.insert_rule(RuleRow(
                    id=0,
                    title=title,
                    keywords=keywords,
                    file_path=rel_path,
                    description=description or None,
                    category=category or None,
                    content_hash=content_hash,
                    content=content,
                    created_by=who,
                    updated_by=who,
                ))
                storage.insert_rule_event(
                    rid, "created", who,
                    content_hash=content_hash, source_repo=src_repo,
                    source_ref=src_ref, source_commit=src_commit,
                )
                indexed += 1

        # Soft-delete orphans (rules whose backing files are gone). Skip
        # rows already archived — re-archiving inflates the count and loses the
        # original archived_at. Compute the full set FIRST so the blast-radius
        # guard (issue #20) can see the whole sweep before any write.
        managed = [
            r for r in storage.list_rules_with_file_paths()
            if r.file_path and not r.archived
        ]

        def _backing_missing(r) -> bool:
            full = Path(r.file_path) if Path(r.file_path).is_absolute() else project_root / r.file_path
            return not full.exists()

        orphans = [r for r in managed if _backing_missing(r)]

        archive_blocked = 0
        if archive_would_storm(len(orphans), len(managed)) and not force:
            # Almost certainly wrong context (bad project_root, empty rules dir).
            # Archive NOTHING; the .md upserts above already committed.
            archive_blocked = len(orphans)
        else:
            for r in orphans:
                storage.soft_delete_rule(r.id)
                storage.insert_rule_event(
                    r.id, "archived", who,
                    source_repo=src_repo, source_ref=src_ref,
                    source_commit=src_commit,
                )
                archived += 1

        # Turn [[slug]] wikilinks into rule->rule relations. Shared with the
        # watcher's sync_once so the stdio-startup path and this tool stay in
        # lockstep. Additive + idempotent.
        links_created = build_wikilink_relations(storage, project_root)

        guard_note = ""
        if archive_blocked:
            guard_note = (
                f" REFUSED to archive {archive_blocked} of {len(managed)} rules "
                f"in one sweep (blast-radius guard #20 — likely wrong "
                f"project_root or an empty rules dir); re-run with force=True "
                f"if this is intentional."
            )

        return _with_nudge(
            f"Sync complete: {indexed} new, {updated} updated, "
            f"{archived} orphans archived, {links_created} links created."
            f"{guard_note}",
            tracker,
        )

    @mcp.tool()
    def reinforce_rule(rule_id: int, actor: str = "") -> str:
        """Deliberately reinforce a rule — signals 'still correct'. Also the
        upgrade path for a rule first imported by 'nobody': a named actor's
        reinforcement gives the row a signed-off event even though
        created_by stays unchanged (issue #10)."""
        tracker.record_call("reinforce_rule")
        who = resolve_actor(actor)
        row = storage.find_by_id(EntityType.RULE, rule_id)
        if row is None:
            return _with_nudge(f"Rule {rule_id} not found.", tracker)

        counters.increment(EntityType.RULE, rule_id, "reinforcement_count")
        counters.increment(EntityType.RULE, rule_id, "last_hit_at")
        storage.insert_rule_event(rule_id, "reinforced", who)
        snap = counters.get(EntityType.RULE, rule_id)
        count = snap.get("reinforcement_count", 0)
        return _with_nudge(
            f"Reinforced: {row.title} (reinforcement_count={count})", tracker,
        )

    @mcp.tool()
    def find_duplicate_rules(threshold: float = 0.9) -> str:
        """Surface NEAR-DUPLICATE rules for review (issue #30). Deterministic
        MinHash/LSH over each active rule's title+keywords+content;
        embedding-free. READ-ONLY — never merges, supersedes, or deletes; a
        human/agent decides what (if anything) to reconcile."""
        tracker.record_call("find_duplicate_rules")
        from ..dedup import find_near_duplicates

        titles: dict[int, str] = {}
        items: list[tuple[int, str]] = []
        for row in storage.iter_entries(EntityType.RULE):
            if getattr(row, "archived", False):
                continue
            if getattr(row, "status", "active") == "superseded":
                continue
            titles[row.id] = row.title
            items.append(
                (row.id, f"{row.title} {row.keywords or ''} {row.content or ''}")
            )

        clusters = find_near_duplicates(items, threshold=threshold)
        if not clusters:
            return _with_nudge("No near-duplicate rules found.", tracker)
        lines = [
            f"Found {len(clusters)} near-duplicate cluster(s) (threshold={threshold}):"
        ]
        for i, cluster in enumerate(clusters, 1):
            lines.append(f"  cluster {i}:")
            for rid in cluster:
                lines.append(f"    #{rid} {titles.get(rid, '')}")
        return _with_nudge("\n".join(lines), tracker)

    @mcp.tool()
    def sift_candidates(candidates: list[dict]) -> str:
        """Server-side tail of the ingest funnel for a REMOTE codebase (issue #72).

        The local `mcm-engine ingest --remote` client walks the repo, extracts
        spans, and applies the rule-like gate; this tool bands each span against
        the LIVE rule corpus (MinHash) and returns the net-new survivors — NOVEL
        (nothing close exists) and REFINE (same subject as an existing rule, body
        diverges; the existing rule id is named). READ-ONLY: nothing is written —
        the agent decides per survivor via `add_rule` (NOVEL) or
        `supersede_rule`/`reinforce_rule` (REFINE).

        `candidates`: `[{"text": "<span>", "source_topic": "<path or symbol>"}, ...]`.
        Files never traverse the wire — only the extracted spans do.

        COMPLEXITY: one MinHash comparison per (span, active rule), so wall time
        is O(spans x rules). Batch spans into small groups (the `ingest --remote`
        client does; default 5); the tool is idempotent and safe to call
        repeatedly. Refuses more than `MCM_SIFT_MAX_SPANS` (default 25) per call
        so a single request can't outrun the transport (issue #76)."""
        tracker.record_call("sift_candidates")
        from ..ingest import rulesift

        raw = candidates or []
        cap = _sift_max_spans()
        if len(raw) > cap:
            return _with_nudge(
                f"sift_candidates: refused {len(raw)} spans — per-call cap is {cap}. "
                f"Batch into groups <= {cap} (the tool is idempotent, so call it "
                f"repeatedly), or raise MCM_SIFT_MAX_SPANS.", tracker)

        existing = rulesift.load_existing_rules(storage)
        spans = [
            ((c or {}).get("text", ""), (c or {}).get("source_topic", ""))
            for c in raw
        ]
        _t0 = time.perf_counter()
        survivors = rulesift.sift_spans(spans, existing)
        log(f"sift_candidates: spans={len(spans)} corpus={len(existing)} "
            f"survivors={len(survivors)} elapsed={time.perf_counter() - _t0:.2f}s")
        if not survivors:
            return _with_nudge(
                f"sift_candidates: {len(spans)} span(s) in, 0 net-new "
                f"(all KNOWN or not rule-shaped).", tracker)

        existing_by_id = dict(existing)
        lines = [
            f"sift_candidates: {len(spans)} span(s) -> {len(survivors)} "
            f"net-new candidate(s):"
        ]
        for i, c in enumerate(survivors, 1):
            head = f"  === candidate {i}/{len(survivors)} [{c.band.value}]"
            if c.source_topic:
                head += f" from {c.source_topic}"
            lines.append(head + " ===")
            if c.band is rulesift.Band.REFINE and c.matched_rule_id:
                snippet = (existing_by_id.get(c.matched_rule_id, "") or "")[:200]
                lines.append(f"  refines rule #{c.matched_rule_id}: {snippet}")
            lines.append("  " + c.text.replace("\n", "\n  "))
        lines.append(
            "  Decide per candidate: add_rule (NOVEL) / "
            "supersede_rule|reinforce_rule (REFINE) / skip.")
        return _with_nudge("\n".join(lines), tracker)

    @mcp.tool()
    def find_conflicting_rules(topic_threshold: float = 0.5,
                               body_threshold: float = 0.4) -> str:
        """Surface CONFLICT candidates (issue #32): active rules that are
        TOPICALLY similar (title+keywords) but whose BODIES diverge — "same
        subject, opposite story", the inverse of a near-duplicate. Deterministic,
        embedding-free. READ-ONLY — never supersedes/merges; a human or agent
        decides what (if anything) to supersede_rule."""
        tracker.record_call("find_conflicting_rules")
        from ..dedup import find_conflicts

        items, titles, importances = _active_conflict_items(storage)
        pairs = find_conflicts(items, topic_threshold=topic_threshold,
                               body_threshold=body_threshold)
        if not pairs:
            return _with_nudge("No conflicting rules found.", tracker)
        lines = [f"Found {len(pairs)} conflict candidate(s) "
                 f"(topic>={topic_threshold}, body<={body_threshold}):"]
        for a, b, label in pairs:
            ta, tb = titles.get(a, ""), titles.get(b, "")
            ia, ib = importances.get(a, 0), importances.get(b, 0)
            # #64 tiebreak: the higher-importance rule is the keeper; the lower
            # yields. Equal importance stays a human/agent call.
            if ia != ib:
                keep, yield_, ik, iy = (a, b, ia, ib) if ia > ib else (b, a, ib, ia)
                tk = titles.get(keep, "")
                ty = titles.get(yield_, "")
                lines.append(
                    f"  [{label}] #{keep} '{tk}' (importance {ik}) OVERRIDES "
                    f"#{yield_} '{ty}' (importance {iy}) — supersede #{yield_}"
                )
            else:
                lines.append(
                    f"  [{label}] #{a} '{ta}'  <->  #{b} '{tb}'  "
                    f"(equal importance {ia} — you decide)"
                )
        lines.append("  Review; if one supersedes the other, call supersede_rule.")
        return _with_nudge("\n".join(lines), tracker)

    @mcp.tool()
    def consolidation_report(max_age_days: int = 90) -> str:
        """Read-only KB-hygiene report (issue #31): near-duplicate merge
        candidates, topic-similar/body-divergent conflict candidates, and stale
        rules (unreinforced + aged + not recently hit). Surfacing only — mutates
        nothing; act via supersede_rule / archive / report_outcome."""
        tracker.record_call("consolidation_report")
        from ..consolidate import consolidation_report as _report
        from ..consolidate import format_report

        return _with_nudge(format_report(_report(storage, max_age_days=max_age_days)),
                           tracker)

    @mcp.tool()
    def report_outcome(rule_ids: list[int], passed: bool, actor: str = "") -> str:
        """Record whether acting on rule(s) actually WORKED (issue #21) — a
        CORRECTNESS signal, kept separate from popularity (hit/reinforcement).

        AUTHOR!=JUDGE guard (load-bearing): a report whose actor is the rule's
        own author is self-certification — the model agreeing with itself. It is
        still logged (rule_outcomes row + event) but does NOT move the
        correctness counters; only an INDEPENDENT actor's report can. Trust keys
        on the author!=judge relationship, not identity alone."""
        tracker.record_call("report_outcome")
        who = resolve_actor(actor)
        results: list[str] = []
        for rid in rule_ids:
            row = storage.find_by_id(EntityType.RULE, rid)
            if row is None:
                results.append(f"{rid}: not found")
                continue
            is_self = bool(row.created_by) and who == row.created_by
            storage.record_outcome(rid, who, passed, count=not is_self)
            if is_self:
                results.append(f"{rid}: recorded (self-report by {who}, uncounted)")
            else:
                results.append(f"{rid}: {'passed' if passed else 'failed'} recorded")
        return _with_nudge("report_outcome — " + "; ".join(results), tracker)

    @mcp.tool()
    def supersede_rule(old_id: int, new_id: int, actor: str = "") -> str:
        """Mark old_id as superseded by new_id (issue #21): soft-expire, never
        delete. A superseded rule drops out of default search but stays
        inspectable (include_superseded / as_of)."""
        tracker.record_call("supersede_rule")
        who = resolve_actor(actor)
        old = storage.find_by_id(EntityType.RULE, old_id)
        if old is None:
            return _with_nudge(f"Rule {old_id} not found.", tracker)
        new = storage.find_by_id(EntityType.RULE, new_id)
        if new is None:
            return _with_nudge(f"Superseding rule {new_id} not found.", tracker)
        storage.supersede_rule(old_id, new_id, who)
        return _with_nudge(
            f"Superseded: {old.title} (now superseded_by={new_id})", tracker,
        )
