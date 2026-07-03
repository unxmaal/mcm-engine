"""Read-only consolidation "sleep" pass report (issue #31).

Composes the deterministic detectors we already ship into one KB-hygiene report:

  - merge_candidates:    near-duplicate clusters (dedup.find_near_duplicates, #30)
  - conflict_candidates: topic-similar / body-divergent pairs (dedup.find_conflicts, #32)
  - stale_candidates:    active rules with no reinforcement, aged past max_age_days,
                         and not hit within max_age_days

v1 MUTATES NOTHING. It flags candidates; acting on them is done with the existing
tools (`supersede_rule`, archive/restore, `report_outcome`, `link_knowledge`).
Opt-in apply/decay/evict is an explicit follow-up. Deterministic: no RNG, and
`now` is injectable so the age math is testable.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional


def _age_days(ts: Any, now: datetime) -> Optional[float]:
    """Age of a timestamp in days relative to ``now``. Accepts datetime, ISO
    string, or None. Handles the tz-aware (Postgres) vs tz-naive (SQLite) mix."""
    if ts is None:
        return None
    if isinstance(ts, str):
        try:
            ts = datetime.fromisoformat(ts)
        except ValueError:
            return None
    n = now
    if ts.tzinfo is not None and n.tzinfo is None:
        n = n.replace(tzinfo=ts.tzinfo)
    elif ts.tzinfo is None and n.tzinfo is not None:
        ts = ts.replace(tzinfo=n.tzinfo)
    return (n - ts).total_seconds() / 86400.0


def consolidation_report(
    storage: Any, *, max_age_days: int = 90, now: Optional[datetime] = None,
) -> dict:
    """Build a read-only KB-hygiene report over ACTIVE rules. Returns a dict:
    ``{summary, merge_candidates, conflict_candidates, stale_candidates}``."""
    from .backends import EntityType
    from .dedup import find_conflicts, find_near_duplicates

    if now is None:
        now = datetime.now()

    rules = [
        r for r in storage.iter_entries(EntityType.RULE)
        if not getattr(r, "archived", False)
        and getattr(r, "status", "active") != "superseded"
    ]
    titles = {r.id: r.title for r in rules}

    # merge candidates: near-duplicates over the whole text (#30)
    dup_items = [
        (r.id, f"{r.title} {r.keywords or ''} {r.content or ''}") for r in rules
    ]
    merge_candidates = [
        [{"id": rid, "title": titles.get(rid, "")} for rid in cluster]
        for cluster in find_near_duplicates(dup_items)
    ]

    # conflict candidates: topic-similar, body-divergent (#32)
    conf_items = [
        (r.id, f"{r.title} {r.keywords or ''}", r.content or "") for r in rules
    ]
    conflict_candidates = [
        {"a": {"id": a, "title": titles.get(a, "")},
         "b": {"id": b, "title": titles.get(b, "")},
         "label": label}
        for a, b, label in find_conflicts(conf_items)
    ]

    # stale candidates: unreinforced, aged, not recently hit (flagged only)
    stale_candidates = []
    for r in rules:
        if (getattr(r, "reinforcement_count", 0) or 0) > 0:
            continue
        created_age = _age_days(getattr(r, "created_at", None), now)
        if created_age is None or created_age <= max_age_days:
            continue
        last_hit_age = _age_days(getattr(r, "last_hit_at", None), now)
        if last_hit_age is not None and last_hit_age <= max_age_days:
            continue
        why = "no reinforcement; created {}d ago; {}".format(
            int(created_age),
            "never hit" if last_hit_age is None else "last hit {}d ago".format(int(last_hit_age)),
        )
        stale_candidates.append({"id": r.id, "title": r.title, "why": why})

    return {
        "summary": {
            "active_rules": len(rules),
            "merge_candidates": len(merge_candidates),
            "conflict_candidates": len(conflict_candidates),
            "stale_candidates": len(stale_candidates),
        },
        "merge_candidates": merge_candidates,
        "conflict_candidates": conflict_candidates,
        "stale_candidates": stale_candidates,
    }


def format_report(rep: dict) -> str:
    """Human-readable rendering of a consolidation_report dict (CLI + MCP tool)."""
    s = rep["summary"]
    lines = [
        "Consolidation report: {} active rules".format(s["active_rules"]),
        "  merge candidates:    {}".format(s["merge_candidates"]),
        "  conflict candidates: {}".format(s["conflict_candidates"]),
        "  stale candidates:    {}".format(s["stale_candidates"]),
    ]
    for cluster in rep["merge_candidates"]:
        members = ", ".join("#{} '{}'".format(m["id"], m["title"]) for m in cluster)
        lines.append("  [merge] " + members)
    for c in rep["conflict_candidates"]:
        lines.append("  [conflict:{}] #{} '{}'  <>  #{} '{}'".format(
            c.get("label", ""), c["a"]["id"], c["a"]["title"],
            c["b"]["id"], c["b"]["title"]))
    for st in rep["stale_candidates"]:
        lines.append("  [stale] #{} '{}' — {}".format(st["id"], st["title"], st["why"]))
    lines.append(
        "(read-only — act via supersede_rule / archive / report_outcome / link_knowledge)")
    return "\n".join(lines)
