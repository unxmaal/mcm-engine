"""Admin plane service logic (issue #64, Phase 3) — pure, no HTTP.

Request-independent functions over a ``StorageBackend`` so the logic is
unit-testable without a socket. The HTTP layer in ``app.py`` is a thin shell
over these.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from .. import hierarchy
from ..backends import EntityType, RuleRow


def _vocab() -> dict:
    return {
        "scopes": list(hierarchy.SCOPES),
        "kinds": list(hierarchy.KINDS),
        "importance_min": hierarchy.IMPORTANCE_MIN,
        "importance_max": hierarchy.IMPORTANCE_MAX,
    }


def _iso(v: Any) -> Optional[str]:
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, str):
        return v
    return None


def serialize_rule(r: RuleRow) -> dict:
    """A JSON-safe view of a rule: the hierarchy axes the admin tunes plus the
    derived signals (hits/reinforcement/correctness) that inform the tuning."""
    return {
        "id": r.id,
        "title": r.title,
        "category": r.category,
        "importance": r.importance,
        "scope": r.scope,
        "kind": r.kind,
        "status": r.status,
        "hit_count": r.hit_count,
        "reinforcement_count": r.reinforcement_count,
        "correct_count": r.correct_count,
        "incorrect_count": r.incorrect_count,
        "pinned": bool(r.pinned),
        "archived": bool(r.archived),
        "file_path": r.file_path,
        "updated_by": r.updated_by,
        "updated_at": _iso(r.updated_at),
    }


def rules_payload(
    storage, *, include_archived: bool = False, min_importance: int = 0,
    limit: Optional[int] = None,
) -> dict:
    """Full rules listing for the grid, importance-first, plus the vocab so the
    frontend builds its dropdowns from the server (one source of allowed
    values) and the ``store`` identity so the admin can see which KB is live."""
    rows = storage.list_rules(
        include_archived=include_archived,
        min_importance=min_importance,
        limit=limit,
    )
    return {
        "store": str(getattr(storage, "identity", "") or ""),
        "count": len(rows),
        "vocab": _vocab(),
        "rules": [serialize_rule(r) for r in rows],
    }


def _node(r: RuleRow) -> dict:
    return {
        "id": r.id,
        "title": r.title,
        "category": r.category,
        "importance": r.importance,
        "scope": r.scope,
        "kind": r.kind,
        "status": r.status,
        "hit_count": r.hit_count,
        "reinforcement_count": r.reinforcement_count,
        "archived": bool(r.archived),
    }


def graph_payload(storage, *, include_archived: bool = False) -> dict:
    """Structure view (issue #64, Phase 3 Display 2): rules as nodes, and the
    rule<->rule rows of the relations table as edges. Only edges whose BOTH
    endpoints are rules present in the node set are included, so no edge dangles
    to a knowledge/error entity or an excluded (archived) rule."""
    rows = storage.list_rules(include_archived=include_archived)
    ids = {r.id for r in rows}
    edges: list[dict] = []
    for rel in storage.iter_relations():
        if (
            rel.source_type == EntityType.RULE
            and rel.target_type == EntityType.RULE
            and rel.source_id in ids
            and rel.target_id in ids
        ):
            edges.append({
                "source": rel.source_id,
                "target": rel.target_id,
                "relation": rel.relation,
                "note": rel.note,
            })
    return {
        "store": str(getattr(storage, "identity", "") or ""),
        "count": len(rows),
        "vocab": _vocab(),
        "nodes": [_node(r) for r in rows],
        "edges": edges,
    }


def apply_metadata(
    storage, rule_id: int, *,
    importance: Optional[int] = None,
    scope: Optional[str] = None,
    kind: Optional[str] = None,
    category: Optional[str] = None,
    actor: str = "admin-ui",
) -> tuple[int, dict]:
    """Apply a tuning edit through the shared write path. Returns an
    ``(http_status, body)`` pair: 200 with the updated rule, 400 with the
    validation error, or 404 if the rule is absent. Never raises on bad input —
    the vocab rejection becomes a 400 the UI can surface inline."""
    try:
        updated = storage.set_rule_metadata(
            rule_id,
            importance=importance,
            scope=scope,
            kind=kind,
            category=category,
            actor=actor,
        )
    except ValueError as e:
        return 400, {"error": str(e)}
    if updated is None:
        return 404, {"error": f"rule not found: #{rule_id}"}
    return 200, {"rule": serialize_rule(updated)}
