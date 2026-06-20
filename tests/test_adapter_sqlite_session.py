"""Conformance for the embedded InMemorySession SessionStore.

Per OQ-5, session state is in-process only; this adapter never touches
disk. The conformance is small: load/save round-trips and isolation.
"""
from __future__ import annotations

from mcm_engine.backends import CONTRACT_VERSION, SessionStore


def test_protocol_runtime_check():
    from mcm_engine.adapters.sqlite.session import InMemorySession
    s = InMemorySession()
    assert isinstance(s, SessionStore)
    assert s.CONTRACT_VERSION == CONTRACT_VERSION


def test_missing_key_returns_none():
    from mcm_engine.adapters.sqlite.session import InMemorySession
    s = InMemorySession()
    assert s.load_state("absent") is None


def test_save_then_load_roundtrip():
    from mcm_engine.adapters.sqlite.session import InMemorySession
    s = InMemorySession()
    s.save_state("tracker", {"turn_count": 7, "last_topic": "abc"})
    assert s.load_state("tracker") == {"turn_count": 7, "last_topic": "abc"}


def test_save_overwrites():
    from mcm_engine.adapters.sqlite.session import InMemorySession
    s = InMemorySession()
    s.save_state("k", {"a": 1})
    s.save_state("k", {"a": 2})
    assert s.load_state("k") == {"a": 2}


def test_load_returns_copy_not_internal_ref():
    """Mutating the returned dict must not mutate the stored state."""
    from mcm_engine.adapters.sqlite.session import InMemorySession
    s = InMemorySession()
    s.save_state("k", {"x": [1, 2, 3]})
    loaded = s.load_state("k")
    loaded["x"] = "tampered"
    again = s.load_state("k")
    assert again == {"x": [1, 2, 3]}


def test_separate_instances_do_not_share_state():
    """In-process means per-instance, not module-global."""
    from mcm_engine.adapters.sqlite.session import InMemorySession
    s1 = InMemorySession()
    s2 = InMemorySession()
    s1.save_state("k", {"v": 1})
    assert s2.load_state("k") is None
