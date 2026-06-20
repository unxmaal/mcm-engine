"""External rules file tools — add_rule, read_rule, promote_to_rule,
sync_rules, reinforce_rule.

Rewired in MCM2-02 (Phase 0): all SQL routes through SqliteStorage /
SqliteCounters. Orphan removal in sync_rules now soft-deletes (sets
archived=1) instead of hard-deleting, matching the watcher-cascade
direction (MCM2-23) and the v7 schema columns.
"""
from __future__ import annotations

import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..backends import EntityType, RuleRow
from ..db import log
from ..files.watcher import compute_content_hash
from ..tracker import SessionTracker
from ..wiring import Context, coerce_context


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

    result: dict[str, str] = {"content_hash": compute_content_hash(content)}
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


def register_rules_tools(
    mcp: FastMCP,
    ctx_or_db,
    tracker: SessionTracker,
    project_name: str,
    rules_paths: list[Path],
    project_root: Path,
) -> None:
    """Register add_rule, read_rule, promote_to_rule, sync_rules,
    reinforce_rule tools.

    Accepts a Context or a raw KnowledgeDB for backward compat.
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
    ) -> str:
        """Create or index a rule file."""
        tracker.record_call("add_rule", topic=title)
        tracker.record_store()

        existing = storage.find_rule_by_title(title)
        if existing is not None:
            storage.update_rule(
                existing.id,
                keywords=keywords,
                description=(content[:500] if content else ""),
                category=category,
                file_path=file_path or existing.file_path,
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
        else:
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

        description = content[:500] if content else ""
        storage.insert_rule(RuleRow(
            id=0,
            title=title,
            keywords=keywords,
            file_path=actual_path or None,
            description=description or None,
            category=category or None,
            content_hash=content_hash,
        ))

        msg = f"Rule added: {title}"
        if actual_path:
            msg += f"\n  File: {actual_path}"
        if warning:
            msg += warning
        return _with_nudge(msg, tracker, title)

    @mcp.tool()
    def read_rule(file_path: str) -> str:
        """Read a rule file's contents. Increments hit_count for tracking."""
        tracker.record_call("read_rule", topic=file_path)

        fp = Path(file_path)
        full = fp if fp.is_absolute() else project_root / file_path
        if not full.exists():
            return _with_nudge(f"Rule file not found: {file_path}", tracker)

        row = storage.find_rule_by_file_path(file_path)
        if row is not None:
            counters.increment(EntityType.RULE, row.id, "hit_count")
            counters.increment(EntityType.RULE, row.id, "last_hit_at")

        try:
            content = full.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return _with_nudge(f"Error reading {file_path}: {e}", tracker)

        return _with_nudge(content, tracker, file_path)

    @mcp.tool()
    def promote_to_rule(
        source_type: str,
        source_id: int,
        title: str,
        category: str = "",
        keywords: str = "",
    ) -> str:
        """Promote a DB entry to a persistent rule file."""
        tracker.record_call("promote_to_rule", topic=title)

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

        return add_rule(
            title=title,
            keywords=keywords,
            content=content,
            category=category,
        )

    @mcp.tool()
    def sync_rules() -> str:
        """Re-index all .md files. Upserts DB entries; archives orphans
        (soft-delete) for files that no longer exist."""
        tracker.record_call("sync_rules")

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
                )
                # Files-win: a reappeared file un-archives its row.
                if existing.archived:
                    storage.restore_rule(existing.id)
                updated += 1
            else:
                storage.insert_rule(RuleRow(
                    id=0,
                    title=title,
                    keywords=keywords,
                    file_path=rel_path,
                    description=description or None,
                    category=category or None,
                    content_hash=content_hash,
                ))
                indexed += 1

        # Soft-delete orphans (rules whose backing files are gone). Skip
        # rows that are already archived — re-archiving inflates the count,
        # loses the original archived_at timestamp, and is functionally
        # a no-op anyway.
        for r in storage.list_rules_with_file_paths():
            fp = r.file_path
            if not fp or r.archived:
                continue
            full = Path(fp) if Path(fp).is_absolute() else project_root / fp
            if not full.exists():
                storage.soft_delete_rule(r.id)
                archived += 1

        return _with_nudge(
            f"Sync complete: {indexed} new, {updated} updated, {archived} orphans archived.",
            tracker,
        )

    @mcp.tool()
    def reinforce_rule(rule_id: int) -> str:
        """Deliberately reinforce a rule — signals 'still correct'."""
        tracker.record_call("reinforce_rule")
        row = storage.find_by_id(EntityType.RULE, rule_id)
        if row is None:
            return _with_nudge(f"Rule {rule_id} not found.", tracker)

        counters.increment(EntityType.RULE, rule_id, "reinforcement_count")
        counters.increment(EntityType.RULE, rule_id, "last_hit_at")
        snap = counters.get(EntityType.RULE, rule_id)
        count = snap.get("reinforcement_count", 0)
        return _with_nudge(
            f"Reinforced: {row.title} (reinforcement_count={count})", tracker,
        )
