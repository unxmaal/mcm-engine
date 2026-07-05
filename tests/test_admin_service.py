"""Admin plane service logic (issue #64, Phase 3) — pure, no HTTP."""
from __future__ import annotations

import pytest

from mcm_engine.admin import service
from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, RelationRow, RuleRow


@pytest.fixture
def storage(db):
    s = SqliteStorage(db=db)
    s.insert_rule(RuleRow(id=0, title="uv rule", keywords="k", content="use uv"))
    s.insert_rule(RuleRow(id=0, title="minecraft port", keywords="k", content="25565"))
    return s


def test_serialize_rule_has_axes_and_signals(storage):
    r = storage.list_rules()[0]
    d = service.serialize_rule(r)
    for key in ("id", "title", "importance", "scope", "kind", "category",
                "status", "hit_count", "reinforcement_count",
                "correct_count", "incorrect_count"):
        assert key in d


def test_rules_payload_shape_and_order(storage):
    inv = storage.find_rule_by_title("uv rule").id
    storage.set_rule_metadata(inv, importance=2, actor="t")
    payload = service.rules_payload(storage)
    assert payload["count"] == 2
    assert "store" in payload
    assert payload["vocab"]["scopes"] and payload["vocab"]["kinds"]
    # importance-first ordering
    assert payload["rules"][0]["title"] == "uv rule"
    assert payload["rules"][0]["importance"] == 2


def test_rules_payload_excludes_archived_by_default(storage):
    gone = storage.find_rule_by_title("minecraft port").id
    storage.soft_delete_rule(gone)
    assert {r["title"] for r in service.rules_payload(storage)["rules"]} == {"uv rule"}
    both = service.rules_payload(storage, include_archived=True)["rules"]
    assert len(both) == 2


def test_apply_metadata_success_updates_and_serializes(storage):
    rid = storage.find_rule_by_title("uv rule").id
    status, body = service.apply_metadata(
        storage, rid, importance=2, scope="universal", kind="directive", actor="eric")
    assert status == 200
    assert body["rule"]["importance"] == 2 and body["rule"]["scope"] == "universal"
    # persisted + audited
    assert storage.find_by_id(EntityType.RULE, rid).kind == "directive"
    assert any(e.event_type == "metadata" for e in storage.list_rule_events(rid))


def test_apply_metadata_invalid_is_400_no_write(storage):
    rid = storage.find_rule_by_title("uv rule").id
    status, body = service.apply_metadata(storage, rid, scope="galactic")
    assert status == 400 and "error" in body
    assert storage.find_by_id(EntityType.RULE, rid).scope == "conditional"


def test_apply_metadata_unknown_is_404(storage):
    status, body = service.apply_metadata(storage, 999999, importance=1)
    assert status == 404 and "error" in body


# ---------------------------------------------------------------------------
# graph_payload (structure view)
# ---------------------------------------------------------------------------


def test_graph_payload_nodes_are_rules(storage):
    g = service.graph_payload(storage)
    assert {n["title"] for n in g["nodes"]} == {"uv rule", "minecraft port"}
    assert "vocab" in g and "store" in g
    n = g["nodes"][0]
    for key in ("id", "importance", "scope", "kind", "category"):
        assert key in n


def test_graph_payload_edges_are_rule_to_rule_only(storage):
    r1 = storage.find_rule_by_title("uv rule").id
    r2 = storage.find_rule_by_title("minecraft port").id
    storage.insert_relation(RelationRow(
        id=0, source_type=EntityType.RULE, source_id=r1,
        target_type=EntityType.RULE, target_id=r2, relation="related"))
    # a rule->knowledge edge must be excluded from the rules graph
    storage.insert_relation(RelationRow(
        id=0, source_type=EntityType.RULE, source_id=r1,
        target_type=EntityType.KNOWLEDGE, target_id=999, relation="fixes"))
    g = service.graph_payload(storage)
    assert g["edges"] == [
        {"source": r1, "target": r2, "relation": "related", "note": None}
    ]


def test_graph_payload_drops_edges_to_excluded_nodes(storage):
    """An edge to an archived rule (absent from the default node set) is not
    emitted, so the frontend never draws a dangling edge."""
    r1 = storage.find_rule_by_title("uv rule").id
    gone = storage.find_rule_by_title("minecraft port").id
    storage.insert_relation(RelationRow(
        id=0, source_type=EntityType.RULE, source_id=r1,
        target_type=EntityType.RULE, target_id=gone, relation="related"))
    storage.soft_delete_rule(gone)
    g = service.graph_payload(storage)  # include_archived defaults False
    assert {n["id"] for n in g["nodes"]} == {r1}
    assert g["edges"] == []
