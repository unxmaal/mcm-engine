"""Retrieval bloat: natural-mode fallback decided across scopes (#201), the
report_error byte budget (#202) and the summary headline cap (#203)."""
from __future__ import annotations

import pytest

from mcm_engine.backends import EntityType, ErrorRow, SearchHit
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tools import search as search_mod
from mcm_engine.tools.knowledge import register_knowledge_tools
from mcm_engine.tools.rules import register_rules_tools
from mcm_engine.tools.search import register_search_tools
from mcm_engine.tracker import NudgeConfig, SessionTracker
from mcm_engine.wiring import coerce_context

_LONG = "Lorem sentence number one is here. " * 30  # ~1,050 chars


class _FakeMCP:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


@pytest.fixture(params=["legacy", "unified"])
def wired(request, tmp_path, monkeypatch):
    monkeypatch.setenv("MCM_ENTRY_MODEL", request.param)
    db = KnowledgeDB(str(tmp_path / "k.db"))
    migrate_core(db)
    mcp = _FakeMCP()
    tracker = SessionTracker(NudgeConfig())
    search_all = register_search_tools(mcp, db, tracker, [], "proj")
    register_knowledge_tools(mcp, db, tracker, "proj", search_all)
    register_rules_tools(mcp, db, tracker, "proj", [tmp_path / "rules"], tmp_path, files_authoritative=False)
    return mcp.tools, coerce_context(db)


class _NaturalBackend:
    """A search backend in natural mode: `strict` ids match every token,
    `loose` ids only match under the OR fallback."""
    query_mode = "natural"

    def __init__(self, strict, loose):
        self.strict, self.loose, self.calls = strict, loose, []

    def _hits(self, etype, fallback):
        self.calls.append((etype, fallback))
        ids = self.strict.get(etype, [])
        if not ids and fallback:
            ids = self.loose.get(etype, [])
        return [SearchHit(entity_type=etype, entity_id=i, score=1.0) for i in ids]

    def search(self, query, *, entity_types=None, limit=10, project=None, caller=None, fallback=True):
        return [h for e in entity_types for h in self._hits(e, fallback)]

    def search_entries(self, query, *, kinds=None, limit=10, project=None, caller=None, fallback=True):
        return [h for k in kinds for h in self._hits(EntityType(k), fallback)]

    def search_plugin(self, scope, query, limit=10, *, caller=None):
        return []


def _seed_pair(tools):
    tools["add_knowledge"](topic="alpha beta", summary="alpha beta gamma delta")
    tools["add_negative"](category="misc", what_failed="alpha only", why_failed="w", correct_approach="f")


def _run(backend, ctx, **kw):
    return search_mod._search_all_scopes(
        backend, ctx.storage, ctx.counters, "alpha beta gamma delta", 10, [],
        min_rank=0.0, project="", **kw)


# ---- #201 ---------------------------------------------------------------------

def test_natural_fallback_is_decided_across_scopes(wired):
    tools, ctx = wired
    _seed_pair(tools)
    backend = _NaturalBackend({EntityType.KNOWLEDGE: [1]}, {EntityType.NEGATIVE: [1]})
    out = _run(backend, ctx)
    assert "[KNOWLEDGE/" in out
    assert "[NEGATIVE #" not in out


def test_natural_fallback_runs_when_every_scope_is_empty(wired):
    tools, ctx = wired
    _seed_pair(tools)
    backend = _NaturalBackend({}, {EntityType.NEGATIVE: [1]})
    out = _run(backend, ctx)
    assert "[NEGATIVE #1]" in out


def test_single_scope_query_keeps_the_adapter_fallback(wired):
    tools, ctx = wired
    _seed_pair(tools)
    backend = _NaturalBackend({}, {EntityType.NEGATIVE: [1]})
    out = _run(backend, ctx, entity_types=frozenset({EntityType.NEGATIVE}))
    assert "[NEGATIVE #1]" in out
    assert backend.calls == [(EntityType.NEGATIVE, True)]


# ---- #203 ---------------------------------------------------------------------

def _hit_block(out, tag):
    return next(b for b in out.split("\n\n") if b.startswith(tag))


def test_knowledge_summary_is_capped_in_summary_mode(wired):
    tools, _ = wired
    tools["add_knowledge"](topic="capme", summary="HEADSTART " + _LONG, tags="t1")
    block = _hit_block(tools["search"]("capme", scope="knowledge"), "[KNOWLEDGE/")
    first_line = block.split("\n")[0]
    assert "HEADSTART" in first_line
    assert len(first_line.encode()) < 400
    assert "\n  Tags: t1" in block
    full = tools["search"]("capme", scope="knowledge", detail="full")
    assert _LONG.strip() in full


def test_negative_headline_is_capped_in_summary_mode(wired):
    tools, _ = wired
    tools["add_negative"](category="capneg", what_failed="HEADSTART " + _LONG, why_failed="w", correct_approach="f")
    block = _hit_block(tools["search"]("capneg", scope="negative"), "[NEGATIVE #")
    assert len(block.split("\n  Why:")[0].encode()) < 400
    assert "\n  Why: w" in block and "\n  Fix: f" in block
    assert _LONG.strip() in tools["search"]("capneg", scope="negative", detail="full")


def test_error_headline_is_capped_in_summary_mode(wired):
    tools, ctx = wired
    ctx.storage.insert_error(ErrorRow(id=0, pattern="caperr HEADSTART " + _LONG, root_cause="rc", fix="fx"))
    block = _hit_block(tools["search"]("caperr", scope="errors"), "[ERROR #")
    assert len(block.split("\n  Root cause:")[0].encode()) < 400
    assert "\n  Root cause: rc" in block
    assert _LONG.strip() in tools["search"]("caperr", scope="errors", detail="full")


def test_rule_description_is_capped_and_drops_the_repeated_heading(wired):
    tools, _ = wired
    tools["add_rule"](title="Caprule title", keywords="caprule", content="# Caprule title\n\nHEADSTART " + _LONG)
    block = _hit_block(tools["search"]("caprule", scope="rules"), "[RULE #")
    assert block.count("Caprule title") == 1
    assert "HEADSTART" in block
    assert len(block.encode()) < 450


def test_short_headlines_are_untouched(wired):
    tools, _ = wired
    tools["add_knowledge"](topic="shorty", summary="one short sentence.")
    assert "shorty: one short sentence." in tools["search"]("shorty", scope="knowledge")


# ---- #202 ---------------------------------------------------------------------

def _seed_error_corpus(tools, ctx):
    for i in range(6):
        tools["add_knowledge"](topic=f"zebracrash note {i}", summary=f"zebracrash summary {i}", detail="KDETAIL " + _LONG)
    for i in range(4):
        tools["add_negative"](category=f"zebracrash{i}", what_failed="zebracrash retry loop",
                              why_failed="NWHY " + _LONG, correct_approach="NFIX " + _LONG)
    for i in range(6):
        tools["add_rule"](title=f"zebracrash rule {i}", keywords="zebracrash", content="zebracrash RBODY " + _LONG)
    ctx.storage.insert_error(ErrorRow(id=0, pattern="zebracrash exploded before", root_cause="ERC " + _LONG, fix="EFIX done"))


def test_report_error_keeps_the_error_fix_whole_and_summarizes_the_rest(wired, monkeypatch):
    tools, ctx = wired
    monkeypatch.setattr(search_mod, "RANK_THRESHOLD", 0.0)
    _seed_error_corpus(tools, ctx)
    out = tools["report_error"]("zebracrash exploded")
    assert "ERC " + _LONG.strip() in out
    assert "KDETAIL" not in out
    assert "get_entry(" in out
    matches = out.split("--- Matching knowledge ---")[1]
    assert matches.index("[ERROR #") < matches.index("[KNOWLEDGE/")


def test_report_error_response_stays_under_its_byte_budget(wired, monkeypatch):
    tools, ctx = wired
    monkeypatch.setattr(search_mod, "RANK_THRESHOLD", 0.0)
    monkeypatch.setenv("MCM_REPORT_ERROR_BUDGET", "3000")
    _seed_error_corpus(tools, ctx)
    out = tools["report_error"]("zebracrash exploded")
    matches = out.split("--- Matching knowledge ---")[1]
    assert "[ERROR #" in matches
    assert len(matches.encode()) < 3000 + 400
    assert "more match" in matches


# ---- env read at call time ----------------------------------------------------

def test_snippet_cap_is_read_when_called_not_at_import(monkeypatch):
    monkeypatch.setenv("MCM_SNIPPET_MAX", "10")
    assert search_mod._format_snippet("x" * 50) == "\n  Match: " + "x" * 10 + "…"
    monkeypatch.setenv("MCM_SNIPPET_MAX", "0")
    assert search_mod._format_snippet("x" * 50) == ""


def test_inline_cap_and_neg_err_cap_are_read_when_called(wired, monkeypatch):
    tools, _ = wired
    for i in range(4):
        tools["add_negative"](category=f"latecap{i}", what_failed="latecap thing",
                              why_failed="WHY " + _LONG, correct_approach="f")
    tools["add_knowledge"](topic="latecap", summary="latecap")
    monkeypatch.setenv("MCM_INLINE_FIELD_MAX", "20")
    monkeypatch.setenv("MCM_NEG_ERR_SUMMARY_CAP", "1")
    out = tools["search"]("latecap")
    assert out.count("[NEGATIVE #") == 1
    why = out.split("\n  Why: ")[1].split("…")[0]
    assert len(why) <= 20
