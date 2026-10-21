"""Unified read path behind MCM_ENTRY_MODEL (issue #181, 4.0 program #176).

Outside check: the legacy and unified paths run in one process on one store
and produce byte-identical tool output for the same calls, including the
facet-only matches (negative why, error root cause, rule category) that the
legacy per-table indexes covered.
"""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.counters import SqliteCounters
from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, ErrorRow, KnowledgeRow, NegativeRow, RuleRow, SearchHit
from mcm_engine.db import KnowledgeDB
from mcm_engine.entries import mirror_entry
from mcm_engine.policies import entry_model, use_unified
from mcm_engine.schema import migrate_core
from mcm_engine.tools import search as search_mod
from mcm_engine.tools.knowledge import register_knowledge_tools
from mcm_engine.tools.relations import _entry_label, register_relations_tools
from mcm_engine.tools.rules import register_rules_tools
from mcm_engine.tools.search import register_search_tools
from mcm_engine.tools.session import register_session_tools
from mcm_engine.tracker import NudgeConfig, SessionTracker


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _seed(s: SqliteStorage) -> dict[str, int]:
    ids = {}
    ids["k1"] = s.insert_knowledge(KnowledgeRow(
        id=0, topic="mahjong timer", summary="turn clock", detail="the body text",
        tags="mj,timer", kind="finding", project="p1", rationale="because",
        references=[{"type": "file", "target": "x.py:1"}]))
    ids["k2"] = s.insert_knowledge(KnowledgeRow(
        id=0, topic="mahjong scoring", summary="old way", kind="decision"))
    s.supersede_knowledge(ids["k2"], ids["k1"])
    ids["n1"] = s.insert_negative(NegativeRow(
        id=0, category="measurement", what_failed="counted sets",
        why_failed="zebra counts hide order " * 20, correct_approach="compare rankings",
        severity="high", project="p1"))
    ids["e1"] = s.insert_error(ErrorRow(
        id=0, pattern="database is locked", context="wal checkpoint",
        root_cause="giraffe contention", fix="retry once", tags="sqlite"))
    ids["r1"] = s.insert_rule(RuleRow(
        id=0, title="Nudge deadlock fix", keywords="nudge,deadlock", content="# body",
        description="desc", category="okapi", file_path="rules/x.md"))
    ids["r2"] = s.insert_rule(RuleRow(id=0, title="Archived nudge", keywords="nudge",
                                      content="b", description="d"))
    s.soft_delete_rule(ids["r2"])
    ids["r3"] = s.insert_rule(RuleRow(id=0, title="Recalled nudge", keywords="nudge", content="b"))
    s._db.execute_write("UPDATE rules SET status = 'recalled' WHERE id = ?", (ids["r3"],))
    s._db.commit()
    with s.transaction():
        mirror_entry(s, EntityType.RULE, ids["r3"])
    ids["r4"] = s.insert_rule(RuleRow(id=0, title="Superseded nudge", keywords="nudge", content="b"))
    s.supersede_rule(ids["r4"], ids["r1"], "t")
    ids["r5"] = s.insert_rule(RuleRow(id=0, title="Linked rule", keywords="unrelatedword",
                                      content="b", description="linked"))
    s.set_pinned(EntityType.RULE, ids["r1"], True)
    s.set_pinned(EntityType.KNOWLEDGE, ids["k1"], True)
    s.record_outcome(ids["r1"], "judge", True)
    s.set_rule_metadata(ids["r1"], importance=2, scope="universal", actor="t")
    return ids


@pytest.fixture
def env(tmp_path):
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    s = SqliteStorage(db=db)
    s.ensure_schema()
    ids = _seed(s)
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=1000, checkpoint_turns=1000, mandatory_stop_turns=2000,
    ))
    mcp = _FakeMCP()
    root = tmp_path / "proj"
    (root / "rules").mkdir(parents=True)
    search_all = register_search_tools(mcp, db, tracker, [])
    register_knowledge_tools(mcp, db, tracker, "proj", search_all)
    register_session_tools(mcp, db, tracker, "proj", [])
    register_rules_tools(mcp, db, tracker, "proj", [root / "rules"], root,
                         files_authoritative=False)
    register_relations_tools(mcp, db, tracker)
    mcp.tools["link_knowledge"](source_type="rule", source_id=ids["r1"], target_type="rule",
                                target_id=ids["r5"], relation="supports")
    return mcp.tools, s, ids


def test_flag_defaults_to_legacy(monkeypatch):
    monkeypatch.delenv("MCM_ENTRY_MODEL", raising=False)
    assert entry_model() == "legacy" and not use_unified()
    monkeypatch.setenv("MCM_ENTRY_MODEL", "UNIFIED")
    assert use_unified()
    monkeypatch.setenv("MCM_ENTRY_MODEL", "bogus")
    assert entry_model() == "legacy"


QUERIES = [
    dict(query="mahjong"),
    dict(query="mahjong", include_archived=True),
    dict(query="mahjong", detail="full"),
    dict(query="mahjong", project="p1"),
    dict(query="mahjong", project="other"),
    dict(query="nudge"),
    dict(query="nudge", include_archived=True),
    dict(query="nudge", scope="rules", detail="full"),
    dict(query="zebra"),                      # negative why_failed only
    dict(query="zebra", detail="full"),
    dict(query="giraffe"),                    # error root_cause only
    dict(query="okapi"),                      # rule category only
    dict(query="locked", scope="errors"),
    dict(query="measurement", scope="negative"),
    dict(query="nothing-matches-this"),
]


@pytest.mark.parametrize("kwargs", QUERIES, ids=[
    "-".join(f"{k}={v}" for k, v in q.items()) for q in QUERIES])
def test_search_output_identical_under_both_models(env, monkeypatch, kwargs):
    tools, _, _ = env

    def run(model):
        monkeypatch.setenv("MCM_ENTRY_MODEL", model)
        # drop the tracker's repeat-query nudge: it counts calls, not models
        return tools["search"](**kwargs).split("\n\n---\n")[0]
    legacy1 = run("legacy")
    unified = run("unified")
    legacy2 = run("legacy")
    assert legacy1 == legacy2, "hit-count bumps reordered the legacy result; fixture too tight"
    _assert_same_hits(legacy2, unified)


def _assert_same_hits(legacy: str, unified: str) -> None:
    """Byte-identical except the backend match snippet: the unified index holds
    every facet in one column, so a facet-only match (error root cause, rule
    category) windows over neighboring facets. The legacy snippet must be
    contained in the unified one; every other line is equal."""
    l_lines, u_lines = legacy.split("\n"), unified.split("\n")
    assert len(l_lines) == len(u_lines), f"line count differs:\n{legacy}\n---\n{unified}"
    for l, u in zip(l_lines, u_lines):
        if l.startswith("  Match: ") and u.startswith("  Match: "):
            assert l[len("  Match: "):] in u, f"snippet {l!r} not within {u!r}"
        else:
            assert l == u


def test_unified_path_is_actually_taken(env, monkeypatch):
    tools, s, ids = env
    monkeypatch.setenv("MCM_ENTRY_MODEL", "unified")
    calls = []
    real = s.find_entry

    def spy(kind, eid):
        calls.append((kind, eid))
        return real(kind, eid)
    monkeypatch.setattr(s, "find_entry", spy)
    # the tools were registered on a Context built from the same db; patch the
    # class method so every instance is covered
    monkeypatch.setattr(SqliteStorage, "find_entry", lambda self, k, i: spy(k, i))
    out = tools["search"](query="mahjong")
    assert "[KNOWLEDGE/FINDING #" in out
    assert ("knowledge", ids["k1"]) in calls


@pytest.mark.parametrize("detail", ["summary", "full"])
def test_formatters_agree_per_kind(env, detail):
    _, s, ids = env
    counters = SqliteCounters(db=s._db)
    cases = [
        (EntityType.KNOWLEDGE, ids["k1"], lambda h: search_mod._score_and_format_knowledge(
            h, s, counters, "p1", 0.7, False, detail)),
        (EntityType.KNOWLEDGE, ids["k2"], lambda h: search_mod._score_and_format_knowledge(
            h, s, counters, "", 0.7, True, detail)),
        (EntityType.RULE, ids["r1"], lambda h: search_mod._score_and_format_rule(
            h, s, counters, False, 0.7, detail)),
        (EntityType.RULE, ids["r2"], lambda h: search_mod._score_and_format_rule(
            h, s, counters, True, 0.7, detail)),
        (EntityType.RULE, ids["r3"], lambda h: search_mod._score_and_format_rule(
            h, s, counters, True, 0.7, detail)),
        (EntityType.NEGATIVE, ids["n1"], lambda h: search_mod._score_and_format_negative(
            h, s, "p1", 0.7, detail)),
        (EntityType.ERROR, ids["e1"], lambda h: search_mod._score_and_format_error(
            h, s, "", 0.7, detail)),
    ]
    for etype, eid, legacy in cases:
        hit = SearchHit(entity_type=etype, entity_id=eid, score=1.0,
                        is_pinned=bool(s.find_by_id(etype, eid).pinned))
        include_archived = eid in (ids["k2"], ids["r2"], ids["r3"])
        project = "p1" if etype in (EntityType.KNOWLEDGE, EntityType.NEGATIVE) and eid != ids["k2"] else ""
        unified = search_mod._score_and_format_entry(
            hit, s, counters, project, 0.7, include_archived, detail)
        expected = legacy(hit)
        assert (unified is None) == (expected is None), f"{etype.value} #{eid}"
        if expected is not None:
            # the recency term reads now() per call, so the composite can
            # differ in the last bits; the text must be byte-identical
            assert unified[1] == expected[1], f"{etype.value} #{eid}"
            assert unified[0] == pytest.approx(expected[0], rel=1e-6), f"{etype.value} #{eid}"
    # recalled is withheld by both even with include_archived
    assert search_mod._score_and_format_entry(
        SearchHit(entity_type=EntityType.RULE, entity_id=ids["r3"], score=1.0),
        s, counters, "", 0.7, True, detail) is None


def test_labels_and_related_agree(env, monkeypatch):
    tools, s, ids = env
    outs = {}
    for model in ("legacy", "unified"):
        monkeypatch.setenv("MCM_ENTRY_MODEL", model)
        outs[model] = (
            [_entry_label(s, et.value, i) for et, i in [
                (EntityType.KNOWLEDGE, ids["k1"]), (EntityType.NEGATIVE, ids["n1"]),
                (EntityType.ERROR, ids["e1"]), (EntityType.RULE, ids["r1"]),
                (EntityType.RULE, 999)]],
            tools["get_related"](entry_type="rule", entry_id=ids["r1"]),
        )
    assert outs["legacy"] == outs["unified"]
    assert outs["legacy"][0][0].startswith("[KNOWLEDGE] mahjong timer: turn clock")
    assert outs["legacy"][0][4] == "[RULE #999]"


def test_promote_to_rule_agrees(env, monkeypatch):
    tools, s, ids = env
    bodies = {}
    for model, src in (("legacy", "n1"), ("unified", "n1"), ("legacy", "e1"), ("unified", "e1"),
                       ("legacy", "k1"), ("unified", "k1")):
        monkeypatch.setenv("MCM_ENTRY_MODEL", model)
        etype = {"n1": "negative", "e1": "error", "k1": "knowledge"}[src]
        tools["promote_to_rule"](source_type=etype, source_id=ids[src],
                                 title=f"promoted {src} {model}")
        row = s.find_rule_by_title(f"promoted {src} {model}")
        bodies[(model, src)] = (row.content, row.keywords, row.category)
    for src in ("n1", "e1", "k1"):
        assert bodies[("legacy", src)] == bodies[("unified", src)]
    assert bodies[("legacy", "n1")][0].startswith("**What failed:** counted sets")
    assert bodies[("legacy", "n1")][1:] == ("measurement", "measurement")
    assert bodies[("legacy", "e1")][0].startswith("**Error:** database is locked")
    assert bodies[("legacy", "k1")][1] == "mj,timer"


def test_facets_are_searchable_only_through_the_unified_index(env, monkeypatch):
    _, s, ids = env
    from mcm_engine.adapters.sqlite.search import SqliteSearch
    search = SqliteSearch(db=s._db)
    hits = search.search_entries("giraffe", kinds={"error"})
    assert [(h.entity_type, h.entity_id) for h in hits] == [(EntityType.ERROR, ids["e1"])]
    assert search.search_entries("giraffe", kinds={"rule"}) == []
    hits = search.search_entries("okapi")
    assert [(h.entity_type, h.entity_id) for h in hits] == [(EntityType.RULE, ids["r1"])]
    # the knowledge kind is indexed like the legacy knowledge_fts did
    assert [h.entity_id for h in search.search_entries("decision", kinds={"knowledge"})] == [ids["k2"]]


def test_facets_text_is_backfilled_for_rows_written_before_the_column(tmp_path):
    path = str(tmp_path / "k.db")
    s = SqliteStorage(db_path=path)
    s.ensure_schema()
    e = s.insert_error(ErrorRow(id=0, pattern="p", root_cause="wombat root"))
    s._db.execute_write("UPDATE entries SET facets_text = NULL WHERE kind = 'error'")
    s._db.commit()
    from mcm_engine.adapters.sqlite.search import SqliteSearch
    s2 = SqliteStorage(db_path=path)
    s2.ensure_schema()
    assert [h.entity_id for h in SqliteSearch(db=s2._db).search_entries("wombat")] == [e]
