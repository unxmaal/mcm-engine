"""Phase 2 storage surface for the rule hierarchy (issue #64).

Two methods the tuning UI and the MCP verbs sit on:

  - list_rules(...): full-column read (RuleRow already carries the hierarchy
    axes + the derived signals hit_count/reinforcement_count/correct/incorrect),
    ordered importance-first so the inflation audit is the default view.
  - set_rule_metadata(...): the ONLY write for the hierarchy axes. Validates
    against the vocab, updates just the given fields, stamps updated_by, and
    emits a `rule_events` row — the audit trail the realtime-colorize UI reads.
"""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, RuleRow


@pytest.fixture
def storage(db):
    return SqliteStorage(db=db)


def _add(storage, title, content="body"):
    return storage.insert_rule(RuleRow(id=0, title=title, keywords="k", content=content))


# ---------------------------------------------------------------------------
# list_rules
# ---------------------------------------------------------------------------


def test_list_rules_returns_all_with_hierarchy_fields(storage):
    _add(storage, "a")
    _add(storage, "b")
    rules = storage.list_rules()
    assert {r.title for r in rules} == {"a", "b"}
    r = rules[0]
    # derived signals + hierarchy axes present on the row
    for attr in ("importance", "scope", "kind", "hit_count",
                 "reinforcement_count", "correct_count", "incorrect_count"):
        assert hasattr(r, attr)


def test_list_rules_ordered_by_importance_desc(storage):
    _add(storage, "low")
    high = _add(storage, "high")
    storage.set_rule_metadata(high, importance=2, actor="me")
    rules = storage.list_rules()
    assert rules[0].title == "high"


def test_list_rules_excludes_archived_by_default(storage):
    _add(storage, "live")
    gone = _add(storage, "gone")
    storage.soft_delete_rule(gone)
    assert {r.title for r in storage.list_rules()} == {"live"}
    assert {r.title for r in storage.list_rules(include_archived=True)} == {"live", "gone"}


def test_list_rules_min_importance_filter(storage):
    _add(storage, "ref")
    inv = _add(storage, "inv")
    storage.set_rule_metadata(inv, importance=2, actor="me")
    assert {r.title for r in storage.list_rules(min_importance=2)} == {"inv"}


def test_list_rules_limit(storage):
    for i in range(5):
        _add(storage, f"r{i}")
    assert len(storage.list_rules(limit=3)) == 3


# ---------------------------------------------------------------------------
# set_rule_metadata
# ---------------------------------------------------------------------------


def test_set_rule_metadata_updates_all_axes(storage):
    rid = _add(storage, "uv")
    got = storage.set_rule_metadata(
        rid, importance=2, scope="universal", kind="directive",
        category="python", actor="eric",
    )
    assert (got.importance, got.scope, got.kind, got.category) == (
        2, "universal", "directive", "python")
    reread = storage.find_by_id(EntityType.RULE, rid)
    assert reread.importance == 2 and reread.scope == "universal"


def test_set_rule_metadata_stamps_updated_by(storage):
    rid = _add(storage, "x")
    storage.set_rule_metadata(rid, importance=1, actor="eric")
    assert storage.find_by_id(EntityType.RULE, rid).updated_by == "eric"


def test_set_rule_metadata_emits_metadata_event(storage):
    rid = _add(storage, "x")
    storage.set_rule_metadata(rid, importance=1, scope="universal", actor="eric")
    events = storage.list_rule_events(rid)
    assert any(e.event_type == "metadata" and e.actor == "eric" for e in events)


def test_set_rule_metadata_partial_leaves_others_at_default(storage):
    rid = _add(storage, "x")
    storage.set_rule_metadata(rid, scope="universal", actor="e")
    r = storage.find_by_id(EntityType.RULE, rid)
    assert r.scope == "universal"
    assert r.importance == 0 and r.kind == "fact"


def test_set_rule_metadata_no_fields_is_noop_no_event(storage):
    rid = _add(storage, "x")
    result = storage.set_rule_metadata(rid, actor="e")
    assert result is not None  # returns the (unchanged) row
    assert storage.list_rule_events(rid) == []


def test_set_rule_metadata_rejects_invalid_scope(storage):
    rid = _add(storage, "x")
    with pytest.raises(ValueError):
        storage.set_rule_metadata(rid, scope="galactic", actor="e")
    # rejected before any write — row and events untouched
    assert storage.find_by_id(EntityType.RULE, rid).scope == "conditional"
    assert storage.list_rule_events(rid) == []


def test_set_rule_metadata_rejects_invalid_kind(storage):
    rid = _add(storage, "x")
    with pytest.raises(ValueError):
        storage.set_rule_metadata(rid, kind="vibe", actor="e")


def test_set_rule_metadata_rejects_out_of_range_importance(storage):
    rid = _add(storage, "x")
    with pytest.raises(ValueError):
        storage.set_rule_metadata(rid, importance=99, actor="e")
