"""Tests for the adjudication layer (Slice 2, fix_ingestion).

Slice 1 produces rule-shaped, net-new candidates. Slice 2 turns a *verdict*
about each candidate into an actual KB write, and provides the provider-agnostic
harness-delegation seam (render a request for the calling agent, parse the
verdicts it returns). The verdict->write path (`commit_verdicts`) is shared with
the Slice 3 standalone model adjudicator, so it is tested hard here.

  - Verdict / Action + validation
  - commit_verdicts: add / refine (supersede) / reinforce / reject, atomic,
    backend-agnostic, resilient to a bad verdict
  - HarnessDelegation.render_request / parse_verdicts
"""
from __future__ import annotations

import pytest

from mcm_engine.adapters.sqlite.counters import SqliteCounters
from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, RuleRow
from mcm_engine.ingest import adjudicate
from mcm_engine.ingest.adjudicate import Action, Verdict
from mcm_engine.ingest.rulesift import Band, RuleCandidate


@pytest.fixture
def store(db):
    return SqliteStorage(db=db), SqliteCounters(db=db)


def _seed_rule(storage, title="Old rule", content="the old body text of the rule") -> int:
    return storage.insert_rule(RuleRow(
        id=0, title=title, keywords="k", description=content[:500],
        content=content, created_by="seed", updated_by="seed",
    ))


# ---------------------------------------------------------------------------
# Verdict validation
# ---------------------------------------------------------------------------


def test_add_verdict_requires_title_and_content():
    assert Verdict(action=Action.ADD, title="", keywords="k", content="body").validate()
    assert Verdict(action=Action.ADD, title="T", keywords="k", content="").validate()
    assert Verdict(action=Action.ADD, title="T", keywords="k", content="body").validate() is None


def test_refine_verdict_requires_target_rule_id():
    assert Verdict(action=Action.REFINE, title="T", keywords="k", content="b").validate()
    assert Verdict(
        action=Action.REFINE, title="T", keywords="k", content="b", target_rule_id=5
    ).validate() is None


def test_reinforce_verdict_requires_target_rule_id():
    assert Verdict(action=Action.REINFORCE).validate()
    assert Verdict(action=Action.REINFORCE, target_rule_id=5).validate() is None


def test_reject_verdict_is_always_valid():
    assert Verdict(action=Action.REJECT).validate() is None


# ---------------------------------------------------------------------------
# commit_verdicts
# ---------------------------------------------------------------------------


def test_add_creates_rule_with_provenance_event(store):
    storage, counters = store
    report = adjudicate.commit_verdicts(
        storage, counters,
        [Verdict(action=Action.ADD, title="Never log secrets",
                 keywords="secrets, logging", content="Never log secrets to stdout.")],
        actor="ingest", source_commit="abc123",
    )
    assert report.created == 1
    row = storage.find_rule_by_title("Never log secrets")
    assert row is not None and (row.content or "").startswith("Never log secrets")
    events = storage.list_rule_events(row.id)
    assert any(e.event_type == "created" and e.source_commit == "abc123" for e in events)


def test_reinforce_increments_and_events(store):
    storage, counters = store
    rid = _seed_rule(storage)
    report = adjudicate.commit_verdicts(
        storage, counters,
        [Verdict(action=Action.REINFORCE, target_rule_id=rid)], actor="ingest",
    )
    assert report.reinforced == 1
    assert counters.get(EntityType.RULE, rid).get("reinforcement_count", 0) == 1
    assert any(e.event_type == "reinforced" for e in storage.list_rule_events(rid))


def test_refine_creates_new_and_supersedes_old(store):
    storage, counters = store
    old_id = _seed_rule(storage, title="Baud rate", content="use 921600 baud")
    report = adjudicate.commit_verdicts(
        storage, counters,
        [Verdict(action=Action.REFINE, target_rule_id=old_id,
                 title="Baud rate (corrected)", keywords="baud",
                 content="use 460800 baud; 921600 fails after the switch")],
        actor="ingest",
    )
    assert report.superseded == 1 and report.created == 1
    old = storage.find_by_id(EntityType.RULE, old_id)
    assert old.status == "superseded" and old.superseded_by is not None
    new = storage.find_by_id(EntityType.RULE, old.superseded_by)
    assert "460800" in (new.content or "")


def test_reject_is_a_noop(store):
    storage, counters = store
    report = adjudicate.commit_verdicts(
        storage, counters, [Verdict(action=Action.REJECT, source_topic="x.py")],
    )
    assert report.rejected == 1 and report.created == 0


def test_one_bad_verdict_does_not_block_the_rest(store):
    """Automated ingest: a single malformed verdict is counted as an error and
    skipped; valid verdicts in the same batch still commit."""
    storage, counters = store
    report = adjudicate.commit_verdicts(
        storage, counters,
        [
            Verdict(action=Action.ADD, title="", keywords="k", content="no title -> error"),
            Verdict(action=Action.ADD, title="Good rule", keywords="k", content="a good body"),
        ],
        actor="ingest",
    )
    assert report.errors == 1
    assert report.created == 1
    assert storage.find_rule_by_title("Good rule") is not None


# ---------------------------------------------------------------------------
# HarnessDelegation — render request / parse verdicts
# ---------------------------------------------------------------------------


def _cand(text, band, topic="a.py", matched=None):
    return RuleCandidate(text=text, source_topic=topic, band=band, matched_rule_id=matched)


def test_render_request_includes_spans_bands_and_return_schema():
    cands = [
        _cand("never commit secrets", Band.NOVEL, "sec.py"),
        _cand("use 460800 baud not 921600", Band.REFINE, "hw.py", matched=7),
    ]
    existing = {7: "Baud rate: use 921600 baud"}
    req = adjudicate.render_request(cands, existing)
    assert "never commit secrets" in req
    assert "novel" in req and "refine" in req
    # REFINE candidate must surface the existing rule body to compare against.
    assert "use 921600 baud" in req and "7" in req
    # The agent needs the verdict schema + action vocabulary.
    for tok in ("action", "add", "refine", "reinforce", "reject"):
        assert tok in req


def test_parse_verdicts_plain_json_array():
    text = (
        '[{"action":"add","title":"T","keywords":"k","content":"body"},'
        '{"action":"reinforce","target_rule_id":7}]'
    )
    verdicts = adjudicate.parse_verdicts(text)
    assert [v.action for v in verdicts] == [Action.ADD, Action.REINFORCE]
    assert verdicts[0].title == "T"
    assert verdicts[1].target_rule_id == 7


def test_parse_verdicts_tolerates_markdown_fences_and_prose():
    text = (
        "Here are my verdicts:\n```json\n"
        '[{"action":"reject","source_topic":"x.py"}]\n'
        "```\nDone."
    )
    verdicts = adjudicate.parse_verdicts(text)
    assert len(verdicts) == 1 and verdicts[0].action == Action.REJECT


def test_parse_verdicts_raises_on_unparseable():
    with pytest.raises(ValueError):
        adjudicate.parse_verdicts("no json here at all")
