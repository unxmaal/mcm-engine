"""Per-session governance isolation + read-only gate hardening (issue #83).

`SessionTracker` is process-global, but one server serves many client sessions.
Sharing one tracker means client A's tool calls advance the nudge/escalation
state that then blocks client B, and a `session_handoff` from one client wipes
the others' counters. `ScopedTracker` routes every call to a per-session tracker
keyed on the FastMCP session object. Separately, read-only query tools must not
advance the write-hygiene gates (so one `ingest --remote` run's `sift_candidates`
burst can't self-escalate and block its own follow-up write).
"""
from __future__ import annotations

import pytest

from mcm_engine.config import NudgeConfig
from mcm_engine.tracker import MandatoryStopError, ScopedTracker, SessionTracker


class _Session:
    """A weak-referenceable stand-in for a FastMCP ServerSession object."""


class _Provider:
    """Controllable key_provider: set `.current` to the 'active' session."""

    def __init__(self):
        self.current = None

    def __call__(self):
        return self.current


def _cfg(**over) -> NudgeConfig:
    base = dict(
        store_reminder_turns=2, checkpoint_turns=3, mandatory_stop_turns=1000,
        nudge_escalation_threshold=2, rules_check_interval=0, periodic_tools={},
    )
    base.update(over)
    return NudgeConfig(**base)


# --- per-session isolation -------------------------------------------------


def test_sessions_have_independent_turn_counts():
    p = _Provider()
    st = ScopedTracker(_cfg(), key_provider=p)
    a, b = _Session(), _Session()

    p.current = a
    st.record_call("search")
    st.record_call("search")
    p.current = b
    st.record_call("search")

    p.current = a
    assert st.turn_count == 2
    p.current = b
    assert st.turn_count == 1


def test_reset_all_only_affects_calling_session():
    p = _Provider()
    st = ScopedTracker(_cfg(), key_provider=p)
    a, b = _Session(), _Session()

    p.current = a
    st.record_call("search")
    p.current = b
    st.record_call("search")
    st.record_call("search")

    p.current = a
    st.reset_all()          # A hands off

    assert st.turn_count == 0
    p.current = b
    assert st.turn_count == 2   # B is untouched


def test_escalation_block_in_one_session_does_not_block_another():
    p = _Provider()
    st = ScopedTracker(_cfg(), key_provider=p)
    a, b = _Session(), _Session()

    # Drive session A into an escalated block: fire a store_reminder, then make
    # non-resolving calls until the ignored count crosses the threshold.
    p.current = a
    st.record_call("noop_tool")
    st.get_nudge()                     # store_reminder fires (deficit >= 2)
    with pytest.raises(MandatoryStopError):
        for _ in range(5):
            st.record_call("noop_tool")
            st.get_nudge()

    # Session B is completely unaffected — its tool calls still succeed.
    p.current = b
    for _ in range(5):
        st.record_call("search")
    assert st.turn_count == 5

    # And A is still blocked (state preserved per-session).
    p.current = a
    with pytest.raises(MandatoryStopError):
        st.record_call("noop_tool")


def test_attribute_write_delegates_to_calling_session():
    # save_snapshot does `tracker.last_checkpoint_turn = tracker.turn_count`.
    p = _Provider()
    st = ScopedTracker(_cfg(), key_provider=p)
    a, b = _Session(), _Session()

    p.current = a
    st.record_call("search")
    st.last_checkpoint_turn = 1          # write must land on A's tracker
    p.current = b
    st.record_call("search")

    assert st.last_checkpoint_turn == 0  # B unaffected
    p.current = a
    assert st.last_checkpoint_turn == 1  # A's value


def test_none_key_uses_one_shared_default_tracker():
    st = ScopedTracker(_cfg(), key_provider=lambda: None)
    st.record_call("search")
    st.record_call("search")
    assert st.turn_count == 2   # both calls hit the same default tracker


def test_plugin_nudge_replayed_into_every_session():
    p = _Provider()
    st = ScopedTracker(_cfg(), key_provider=p)
    a = _Session()
    p.current = a
    st.record_call("search")            # create A's tracker first

    seen = []
    st.register_plugin_nudge(lambda tr: seen.append(tr) or "PLUGIN NUDGE")

    # replayed into the already-existing A ...
    p.current = a
    assert "PLUGIN NUDGE" in (st.get_nudge() or "")
    # ... and injected into a brand-new session B
    b = _Session()
    p.current = b
    assert "PLUGIN NUDGE" in (st.get_nudge() or "")


def test_sessions_evict_when_key_is_garbage_collected():
    import gc

    p = _Provider()
    st = ScopedTracker(_cfg(), key_provider=p)
    a = _Session()
    p.current = a
    st.record_call("search")
    assert len(st._trackers) == 1

    p.current = None
    del a
    gc.collect()
    assert len(st._trackers) == 0   # WeakKeyDictionary dropped it


# --- read-only gate hardening (secondary) ----------------------------------


def test_read_only_tools_do_not_advance_write_gates():
    t = SessionTracker(_cfg())
    for _ in range(10):
        t.record_call("sift_candidates")
        t.get_nudge()

    assert t.turn_count == 10
    # write-hygiene deficits stayed flat, so nothing fired to escalate
    assert t.turn_count - t.last_store_turn == 0
    assert t.turn_count - t.last_checkpoint_turn == 0
    assert not t.pending_nudges
    # the caller's own follow-up write is NOT blocked
    t.record_call("import_rules")


def test_read_only_burst_never_escalates_even_with_pending_nudge():
    t = SessionTracker(_cfg())
    # Make a real deficit fire from a write-ish tool, so a nudge is pending.
    t.record_call("noop_tool")
    t.record_call("noop_tool")
    t.get_nudge()
    assert t.pending_nudges
    # A long read burst must not accrue ignores or block.
    for _ in range(10):
        t.record_call("list_rules")
    assert t.ignored_counts == {} or all(v == 0 for v in t.ignored_counts.values())


def test_search_still_counts_toward_store_gate():
    t = SessionTracker(_cfg(store_reminder_turns=2))
    t.record_call("search")
    n1 = t.get_nudge()
    t.record_call("search")
    n2 = t.get_nudge()
    assert "REMINDER" in (n2 or "")   # search is NOT read-only; the gate advances
