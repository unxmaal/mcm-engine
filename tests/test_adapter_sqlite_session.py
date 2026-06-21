"""Embedded InMemorySession SessionStore — pinned to SessionConformance.

Per OQ-5, session state is in-process only; the InMemorySession adapter
never touches disk. The shared conformance covers the load/save contract;
the SQLite-specific separate-instance-isolation test stays here since
"in-process" is the embedded adapter's defining trait, not a contract
guarantee for remote SessionStores (which intentionally SHARE state).
"""
from __future__ import annotations

import pytest

from mcm_engine.testing.conformance import SessionConformance


class TestInMemorySession(SessionConformance):
    @pytest.fixture
    def session_store(self):
        from mcm_engine.adapters.sqlite.session import InMemorySession
        return InMemorySession()


def test_separate_instances_do_not_share_state():
    """In-process means per-instance, not module-global. SQLite-/embedded-
    only: a Redis-backed SessionStore would intentionally share."""
    from mcm_engine.adapters.sqlite.session import InMemorySession

    s1 = InMemorySession()
    s2 = InMemorySession()
    s1.save_state("k", {"v": 1})
    assert s2.load_state("k") is None
