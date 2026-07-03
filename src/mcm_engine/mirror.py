"""One-way DB -> git review mirror of active rules (issue #22).

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
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

# Commit with an explicit identity so it works on a fresh box / container / CI
# where no git user is configured.
_GIT_IDENTITY = [
    "-c", "user.email=mcm-engine-mirror@localhost",
    "-c", "user.name=mcm-engine mirror",
]


def export_mirror(storage: Any, out_dir: str | Path) -> dict:
    """Render every ACTIVE rule to ``<out_dir>/rules/<category>/<slug>.md`` and
    commit the result. Returns ``{"written": int, "committed": bool}``.

    Idempotent: a run with no net change makes no commit (``committed=False``).
    """
    from .backends import EntityType
    from .tools.rules import _generate_rule_content, _slugify

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    if not (out / ".git").exists():
        subprocess.run(["git", "init", "-q", str(out)], check=True)

    rules_root = out / "rules"
    # Rewrite the managed subtree from scratch so supersessions/deletions show up
    # as removals in the diff (`git add -A` then picks them up).
    if rules_root.exists():
        shutil.rmtree(rules_root)

    written = 0
    for row in storage.iter_entries(EntityType.RULE):
        if getattr(row, "archived", False):
            continue
        if getattr(row, "status", "active") == "superseded":
            continue
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

    subprocess.run(["git", "-C", str(out), "add", "-A"], check=True)
    has_changes = subprocess.run(
        ["git", "-C", str(out), "diff", "--cached", "--quiet"]
    ).returncode != 0
    committed = False
    if has_changes:
        subprocess.run(
            ["git", "-C", str(out), *_GIT_IDENTITY,
             "commit", "-q", "-m", f"mirror: {written} active rules"],
            check=True,
        )
        committed = True
    return {"written": written, "committed": committed}
