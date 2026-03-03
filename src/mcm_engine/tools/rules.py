"""External rules file tools — add_rule, read_rule, promote_to_rule, sync_rules."""
from __future__ import annotations

import re
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from ..db import KnowledgeDB, log
from ..tracker import SessionTracker


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    text = text.strip("-")
    return text or "untitled"


def _parse_rule_file(path: Path) -> dict:
    """Extract metadata from a rule markdown file.

    Expected format:
        # Title
        **Keywords:** kw1, kw2
        **Category:** cat
        Body text...

    Returns dict with title, keywords, category, description.
    """
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}

    result: dict[str, str] = {}
    lines = content.split("\n")

    # Title from first # heading
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            result["title"] = stripped[2:].strip()
            break

    # Keywords and Category from **Key:** value lines
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

    # Description: first non-empty, non-metadata body paragraph
    in_body = False
    desc_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not in_body:
            # Skip title and metadata lines
            if stripped.startswith("# ") and not stripped.startswith("## "):
                continue
            if re.match(r"\*\*(Keywords?|Category):\*\*", stripped, re.IGNORECASE):
                continue
            if stripped == "":
                continue
            in_body = True
        if in_body:
            if stripped == "" and desc_lines:
                break  # End of first paragraph
            if stripped.startswith("## "):
                break  # Hit a subsection
            desc_lines.append(stripped)

    if desc_lines:
        result["description"] = " ".join(desc_lines)

    return result


def _generate_rule_content(title: str, keywords: str, category: str, content: str) -> str:
    """Generate a rule markdown file."""
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
    db: KnowledgeDB,
    tracker: SessionTracker,
    project_name: str,
    rules_paths: list[Path],
    project_root: Path,
) -> None:
    """Register add_rule, read_rule, promote_to_rule, sync_rules tools.

    Args:
        rules_paths: List of rules directories. The first is the primary
            directory where new rule files are created. All are scanned
            by sync_rules.
    """
    # Primary path for creating new files; all paths for scanning
    primary_rules_path = rules_paths[0] if rules_paths else project_root / "rules"

    @mcp.tool()
    def add_rule(
        title: str,
        keywords: str,
        content: str = "",
        category: str = "",
        file_path: str = "",
    ) -> str:
        """Create or index a rule file. If file_path is empty, creates a new file
        under rules/{category}/{slug}.md. If file_path is provided, indexes an
        existing file.

        Args:
            title: Rule title
            keywords: Comma-separated search keywords
            content: Rule body text (used when creating a new file)
            category: Rule category (used for directory organization)
            file_path: Relative path to existing rule file (indexes it if provided)
        """
        tracker.record_call("add_rule", topic=title)
        tracker.record_store()

        # Check for duplicate by title
        existing = db.execute(
            "SELECT id, file_path FROM rules WHERE title = ?", (title,)
        ).fetchone()
        if existing:
            # Update existing rule
            db.execute_write(
                "UPDATE rules SET keywords = ?, description = ?, category = ?, "
                "file_path = ?, updated_at = datetime('now') WHERE id = ?",
                (keywords, content[:500] if content else "", category,
                 file_path or existing["file_path"], existing["id"]),
            )
            db.commit()
            return _with_nudge(
                f"Updated existing rule: {title} (id={existing['id']})",
                tracker, title,
            )

        actual_path: str = file_path
        warning = ""

        if file_path:
            # Index an existing file
            full = project_root / file_path
            if full.exists():
                parsed = _parse_rule_file(full)
                if not content and parsed.get("description"):
                    content = parsed["description"]
            else:
                warning = f"\nWarning: file '{file_path}' does not exist. Rule indexed without file backing."
        else:
            # Create a new rule file
            cat_dir = primary_rules_path / category if category else primary_rules_path
            cat_dir.mkdir(parents=True, exist_ok=True)
            slug = _slugify(title)
            new_file = cat_dir / f"{slug}.md"

            # Avoid overwriting
            counter = 1
            while new_file.exists():
                new_file = cat_dir / f"{slug}-{counter}.md"
                counter += 1

            file_content = _generate_rule_content(
                title, keywords, category, content or ""
            )
            new_file.write_text(file_content, encoding="utf-8")
            actual_path = str(new_file.relative_to(project_root))

        description = content[:500] if content else ""
        db.execute_write(
            "INSERT INTO rules (title, keywords, file_path, description, category) "
            "VALUES (?, ?, ?, ?, ?)",
            (title, keywords, actual_path, description, category),
        )
        db.commit()

        msg = f"Rule added: {title}"
        if actual_path:
            msg += f"\n  File: {actual_path}"
        if warning:
            msg += warning
        return _with_nudge(msg, tracker, title)

    @mcp.tool()
    def read_rule(file_path: str) -> str:
        """Read a rule file's contents. Increments hit_count for tracking.

        Args:
            file_path: Relative path to the rule file
        """
        tracker.record_call("read_rule", topic=file_path)

        fp = Path(file_path)
        full = fp if fp.is_absolute() else project_root / file_path
        if not full.exists():
            return _with_nudge(f"Rule file not found: {file_path}", tracker)

        # Increment hit count and record last_hit_at
        db.execute_write(
            "UPDATE rules SET hit_count = hit_count + 1, "
            "last_hit_at = datetime('now'), updated_at = datetime('now') "
            "WHERE file_path = ?",
            (file_path,),
        )
        db.commit()

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
        """Promote a DB entry (knowledge, negative, or error) to a persistent rule file.

        Args:
            source_type: 'knowledge', 'negative', or 'error'
            source_id: ID of the source entry
            title: Title for the new rule
            category: Rule category
            keywords: Comma-separated keywords (auto-extracted if empty)
        """
        tracker.record_call("promote_to_rule", topic=title)

        # Fetch source entry
        if source_type == "knowledge":
            row = db.execute(
                "SELECT topic, summary, detail, tags FROM knowledge WHERE id = ?",
                (source_id,),
            ).fetchone()
            if not row:
                return _with_nudge(f"Knowledge entry {source_id} not found.", tracker)
            content = row["summary"]
            if row["detail"]:
                content += f"\n\n{row['detail']}"
            if not keywords:
                keywords = row["tags"] or row["topic"]

        elif source_type == "negative":
            row = db.execute(
                "SELECT category, what_failed, why_failed, correct_approach "
                "FROM negative_knowledge WHERE id = ?",
                (source_id,),
            ).fetchone()
            if not row:
                return _with_nudge(f"Negative knowledge entry {source_id} not found.", tracker)
            content = f"**What failed:** {row['what_failed']}"
            if row["why_failed"]:
                content += f"\n\n**Why:** {row['why_failed']}"
            if row["correct_approach"]:
                content += f"\n\n## Fix\n\n{row['correct_approach']}"
            if not keywords:
                keywords = row["category"]
            if not category:
                category = row["category"]

        elif source_type == "error":
            row = db.execute(
                "SELECT pattern, context, root_cause, fix FROM errors WHERE id = ?",
                (source_id,),
            ).fetchone()
            if not row:
                return _with_nudge(f"Error entry {source_id} not found.", tracker)
            content = f"**Error:** {row['pattern']}"
            if row["context"]:
                content += f"\n\n**Context:** {row['context']}"
            if row["root_cause"]:
                content += f"\n\n**Root cause:** {row['root_cause']}"
            if row["fix"]:
                content += f"\n\n## Fix\n\n{row['fix']}"
            if not keywords:
                keywords = row["pattern"][:100]

        else:
            return _with_nudge(
                f"Invalid source_type '{source_type}'. Use 'knowledge', 'negative', or 'error'.",
                tracker,
            )

        # Delegate to add_rule
        return add_rule(
            title=title,
            keywords=keywords,
            content=content,
            category=category,
        )

    @mcp.tool()
    def sync_rules() -> str:
        """Re-index all .md files across all configured rules directories.
        Upserts DB entries and removes orphans for files that no longer exist.
        """
        tracker.record_call("sync_rules")

        # Collect .md files from all rules paths
        md_files: list[Path] = []
        missing_paths: list[str] = []
        for rp in rules_paths:
            if rp.exists():
                md_files.extend(sorted(rp.rglob("*.md")))
            else:
                missing_paths.append(str(rp))

        if not md_files and missing_paths:
            return _with_nudge(
                f"No rules directories found: {', '.join(missing_paths)}", tracker
            )

        indexed = 0
        updated = 0
        removed = 0

        for md_file in md_files:
            try:
                rel_path = str(md_file.relative_to(project_root))
            except ValueError:
                # External path (not under project_root) — store absolute
                rel_path = str(md_file)
            parsed = _parse_rule_file(md_file)
            if not parsed.get("title"):
                continue  # Skip files without a title heading

            title = parsed["title"]
            keywords = parsed.get("keywords", "")
            category = parsed.get("category", "")
            description = parsed.get("description", "")

            existing = db.execute(
                "SELECT id FROM rules WHERE file_path = ?", (rel_path,)
            ).fetchone()

            if existing:
                db.execute_write(
                    "UPDATE rules SET title = ?, keywords = ?, description = ?, "
                    "category = ?, updated_at = datetime('now') WHERE id = ?",
                    (title, keywords, description, category, existing["id"]),
                )
                updated += 1
            else:
                db.execute_write(
                    "INSERT INTO rules (title, keywords, file_path, description, category) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (title, keywords, rel_path, description, category),
                )
                indexed += 1

        # Remove orphans: DB entries whose files no longer exist
        all_rules = db.execute("SELECT id, file_path FROM rules WHERE file_path IS NOT NULL").fetchall()
        for rule in all_rules:
            fp = rule["file_path"]
            if fp:
                # Absolute paths stored as-is; relative paths resolved against project_root
                full = Path(fp) if Path(fp).is_absolute() else project_root / fp
                if not full.exists():
                    db.execute_write("DELETE FROM rules WHERE id = ?", (rule["id"],))
                    removed += 1

        db.commit()

        return _with_nudge(
            f"Sync complete: {indexed} new, {updated} updated, {removed} orphans removed.",
            tracker,
        )
