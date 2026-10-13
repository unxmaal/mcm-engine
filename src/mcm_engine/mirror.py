"""One-way DB -> git review mirror of active rules (issue #22, remote #78).

Replays the rules store into a git repo of markdown for human review
(`git blame` / diff / PR). It is:

  - READ-ONLY on the store — only enumerates via `iter_entries`; calls no
    storage mutator.
  - STRUCTURALLY ONE-WAY — writes only into the external git dir, never into
    `rules_paths`, so it can never re-trigger the `sync_rules` orphan-archive
    sweep.
  - ACTIVE-ONLY — archived and superseded rules (issue #21) are excluded, so the
    mirror never presents a dead rule as authoritative.

This is a current-state snapshot (not per-event replay): historical bodies are
not stored per event, so per-event commits would show current bodies under
historical commits. `git log` on the mirror is the review/audit surface.

The rendering (`render_rules_to_tree`) and the commit (`git_commit_tree`) are
factored out so the direct `export_mirror` and the MCP-native `export-mirror
--remote` client (issue #78) render the SAME tree the same way — the latter
feeds rows paged over MCP instead of read from a local store.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any, Iterable

# Commit with an explicit identity so it works on a fresh box / container / CI
# where no git user is configured.
_GIT_IDENTITY = [
    "-c", "user.email=mcm-engine-mirror@localhost",
    "-c", "user.name=mcm-engine mirror",
]


def render_rules_to_tree(rows: Iterable[Any], out_dir: str | Path) -> int:
    """Rewrite ``<out_dir>/rules/<category>/<slug>.md`` from `rows` and return
    the count written. Each row needs ``.title``, ``.keywords``, ``.category``,
    ``.content`` (a RuleRow, or any object/namespace with those attrs — the
    remote client wraps the paged JSON this way). Rows are assumed ALREADY
    filtered to active; the managed subtree is rewritten from scratch so
    removals show up in the diff. No git here (see `git_commit_tree`)."""
    from .tools.rules import _generate_rule_content, _slugify

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    rules_root = out / "rules"
    if rules_root.exists():
        shutil.rmtree(rules_root)

    written = 0
    for row in rows:
        category = (row.category or "uncategorized").strip() or "uncategorized"
        slug = _slugify(row.title)
        body = _generate_rule_content(
            row.title, row.keywords, row.category, row.content or ""
        )
        dest = rules_root / category / f"{slug}.md"
        n = 1
        while dest.exists():  # distinct rules that slug-collide within a category
            dest = rules_root / category / f"{slug}-{n}.md"
            n += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(body, encoding="utf-8")
        written += 1
    return written


def git_commit_tree(out_dir: str | Path, written: int) -> bool:
    """git-init `out_dir` if needed, stage everything, and commit iff the tree
    changed. Returns whether a commit was made (idempotent: no net change ->
    no commit). Needs no pre-configured git identity (one is supplied)."""
    out = Path(out_dir)
    if not (out / ".git").exists():
        subprocess.run(["git", "init", "-q", str(out)], check=True)
    subprocess.run(["git", "-C", str(out), "add", "-A"], check=True)
    has_changes = subprocess.run(
        ["git", "-C", str(out), "diff", "--cached", "--quiet"]
    ).returncode != 0
    if has_changes:
        subprocess.run(
            ["git", "-C", str(out), *_GIT_IDENTITY,
             "commit", "-q", "-m", f"mirror: {written} active rules"],
            check=True,
        )
    return has_changes


def is_active_rule(row: Any) -> bool:
    """The single definition of "a rule the mirror exports": not archived, not
    superseded. Shared by the direct `export_mirror` and the remote
    `export_rules` MCP tool so the two export paths cannot drift (gauntlet
    class 1: one policy, one place)."""
    if getattr(row, "archived", False):
        return False
    if getattr(row, "status", "active") == "superseded":
        return False
    return True


def _active_rules(storage: Any) -> Iterable[Any]:
    from .backends import EntityType

    for row in storage.iter_entries(EntityType.RULE):
        if is_active_rule(row):
            yield row


def export_mirror(storage: Any, out_dir: str | Path) -> dict:
    """Render every ACTIVE rule to ``<out_dir>/rules/<category>/<slug>.md`` and
    commit the result. Returns ``{"written": int, "committed": bool}``.

    Idempotent: a run with no net change makes no commit (``committed=False``).
    """
    written = render_rules_to_tree(_active_rules(storage), out_dir)
    committed = git_commit_tree(out_dir, written)
    return {"written": written, "committed": committed}
