"""Issue #14 — import_rules: pod-native bulk rule import.

Tool-layer behavior against the SQLite reference. The transaction() primitive
that makes the batch atomic is exercised on BOTH adapters by the
StorageConformance suite (test_transaction_* there), so the adapter-parity
concern (issue test #11) is covered without a bespoke dual-adapter diff here —
import_rules itself is adapter-agnostic tool code.
"""
from __future__ import annotations

import pytest

from mcm_engine.backends import RuleRow
from mcm_engine.config import NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.schema import migrate_core
from mcm_engine.tracker import SessionTracker
from mcm_engine.tools.rules import register_rules_tools


class FakeMCP:
    def __init__(self):
        self._tools = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator

    def __getitem__(self, name):
        return self._tools[name]


@pytest.fixture
def wired(tmp_path):
    from mcm_engine.adapters.sqlite.storage import SqliteStorage

    db = KnowledgeDB(tmp_path / "rules.db")
    migrate_core(db)
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()

    mcp = FakeMCP()
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=100, checkpoint_turns=100,
        mandatory_stop_turns=100000, nudge_escalation_threshold=3,
    ))
    register_rules_tools(
        mcp, db, tracker, project_name="t",
        rules_paths=[rules_dir], project_root=tmp_path,
    )
    return {
        "import_rules": mcp["import_rules"],
        "add_rule": mcp["add_rule"],
        "storage": SqliteStorage(db=db),
        "tracker": tracker,
    }


def _rule(title, content="body", keywords="kw", **extra):
    return {"title": title, "keywords": keywords, "content": content, **extra}


def _events(storage, title):
    row = storage.find_rule_by_title(title)
    return storage.list_rule_events(row.id) if row else []


def _count_rules(storage):
    # No list-all on the protocol; probe via file-path listing + titles we know.
    return storage


# --- 1. empty batch ------------------------------------------------------


def test_empty_batch_no_writes_no_error(wired):
    res = wired["import_rules"](rules=[])
    assert res == {"total": 0, "created": 0, "updated": 0,
                   "skipped": 0, "errors": 0, "rules": []}


# --- 2. all-new batch ----------------------------------------------------


def test_all_new_batch_creates_rows_and_events(wired):
    res = wired["import_rules"](
        rules=[_rule("A"), _rule("B"), _rule("C")],
        source_commit="deadbeef",
    )
    assert res["total"] == 3
    assert res["created"] == 3
    assert res["updated"] == 0 and res["skipped"] == 0 and res["errors"] == 0
    assert {r["status"] for r in res["rules"]} == {"created"}

    for title in ("A", "B", "C"):
        evs = _events(wired["storage"], title)
        assert [e.event_type for e in evs] == ["created"]
        assert evs[0].source_commit == "deadbeef"


# --- 3. mixed batch, on_duplicate=update ---------------------------------


def test_update_mode_created_updated_and_unchanged(wired):
    wired["add_rule"](title="Existing", keywords="kw", content="old body")

    res = wired["import_rules"](rules=[
        _rule("Existing", content="new body"),   # material change -> updated
        _rule("Fresh", content="brand new"),      # -> created
    ])
    assert res["created"] == 1
    assert res["updated"] == 1
    status = {r["title"]: r["status"] for r in res["rules"]}
    assert status == {"Existing": "updated", "Fresh": "created"}
    assert wired["storage"].find_rule_by_title("Existing").content == "new body"
    # created event (add_rule) + updated event (import); list_rule_events is
    # newest-first.
    assert [e.event_type for e in _events(wired["storage"], "Existing")] == \
        ["updated", "created"]


def test_update_mode_identical_content_is_noop(wired):
    wired["add_rule"](title="Same", keywords="kw", content="identical")
    res = wired["import_rules"](rules=[_rule("Same", content="identical")])
    assert res["updated"] == 0
    assert res["skipped"] == 1
    assert res["rules"][0]["status"] == "skipped"
    # No second event: the re-import didn't change the body.
    assert [e.event_type for e in _events(wired["storage"], "Same")] == ["created"]


# --- 4. on_duplicate=skip ------------------------------------------------


def test_skip_mode_leaves_existing_untouched(wired):
    wired["add_rule"](title="Keep", keywords="kw", content="original")
    res = wired["import_rules"](
        rules=[_rule("Keep", content="would-overwrite"), _rule("NewOne")],
        on_duplicate="skip",
    )
    assert res["skipped"] == 1 and res["created"] == 1
    assert wired["storage"].find_rule_by_title("Keep").content == "original"
    assert [e.event_type for e in _events(wired["storage"], "Keep")] == ["created"]


# --- 5. on_duplicate=error rolls back the whole batch --------------------


def test_error_mode_aborts_and_rolls_back(wired):
    wired["add_rule"](title="Clash", keywords="kw", content="here")

    res = wired["import_rules"](
        rules=[_rule("WouldCreate"), _rule("Clash", content="x")],
        on_duplicate="error",
    )
    assert "error" in res
    assert res["errors"] == 1
    status = {r["title"]: r["status"] for r in res["rules"]}
    assert status["Clash"] == "error"
    # Rollback: the non-colliding row must NOT have landed, and Clash is
    # unchanged with only its original event.
    assert wired["storage"].find_rule_by_title("WouldCreate") is None
    assert wired["storage"].find_rule_by_title("Clash").content == "here"
    assert [e.event_type for e in _events(wired["storage"], "Clash")] == ["created"]


# --- 6. validation: missing field aborts the whole batch -----------------


def test_missing_content_aborts_batch(wired):
    res = wired["import_rules"](rules=[
        _rule("Good"),
        {"title": "Bad", "keywords": "kw"},  # no content
    ])
    assert "error" in res and "content" in res["error"]
    assert res["created"] == 0
    # Nothing landed — not even the valid row before the bad one.
    assert wired["storage"].find_rule_by_title("Good") is None


def test_missing_title_aborts_batch(wired):
    res = wired["import_rules"](rules=[{"keywords": "kw", "content": "c"}])
    assert "error" in res and "title" in res["error"]


# --- 7. duplicate titles within the batch --------------------------------


def test_duplicate_titles_within_batch_abort(wired):
    res = wired["import_rules"](rules=[_rule("Dup"), _rule("Dup", content="other")])
    assert "error" in res and "duplicate" in res["error"].lower()
    assert wired["storage"].find_rule_by_title("Dup") is None


# --- 8. actor resolution -------------------------------------------------


def test_actor_explicit_wins(wired):
    wired["import_rules"](rules=[_rule("Attr")], actor="alice")
    assert _events(wired["storage"], "Attr")[0].actor == "alice"


def test_actor_falls_back_to_env(wired, monkeypatch):
    monkeypatch.setenv("MCM_ACTOR", "env-bob")
    wired["import_rules"](rules=[_rule("Attr2")])
    assert _events(wired["storage"], "Attr2")[0].actor == "env-bob"


def test_actor_terminal_fallback_is_nobody(wired, monkeypatch):
    monkeypatch.delenv("MCM_ACTOR", raising=False)
    wired["import_rules"](rules=[_rule("Attr3")])
    assert _events(wired["storage"], "Attr3")[0].actor == "nobody"


# --- 9. tracker: one call for the whole batch ----------------------------


def test_batch_counts_as_one_tracked_call(wired):
    tracker = wired["tracker"]
    before = tracker.turn_count
    big = [_rule(f"R{i}") for i in range(200)]
    wired["import_rules"](rules=big)
    assert tracker.turn_count == before + 1  # not +200


# --- 10. provenance propagates to every event ----------------------------


def test_source_metadata_propagates_to_all_events(wired):
    wired["import_rules"](
        rules=[_rule("P1"), _rule("P2")],
        source_repo="unxmaal/mcm-engine",
        source_ref="refs/heads/issue_14",
        source_commit="cafe1234",
    )
    for title in ("P1", "P2"):
        ev = _events(wired["storage"], title)[0]
        assert ev.source_repo == "unxmaal/mcm-engine"
        assert ev.source_ref == "refs/heads/issue_14"
        assert ev.source_commit == "cafe1234"


# --- registration smoke: real FastMCP accepts the dict-returning tool ----


def test_registers_on_real_fastmcp(tmp_path):
    from mcm_engine.config import MCMConfig
    from mcm_engine.server import MCMServer

    cfg = MCMConfig(project_name="reg", db_path=str(tmp_path / "reg.db"))
    server = MCMServer(cfg, project_root=tmp_path)
    tool_names = {t.name for t in server.mcp._tool_manager.list_tools()}
    assert "import_rules" in tool_names
