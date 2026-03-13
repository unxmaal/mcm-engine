"""Tests for SessionTracker — nudge thresholds, topic frequency."""
import pytest

from mcm_engine.config import NudgeConfig
from mcm_engine.tracker import MandatoryStopError, SessionTracker


class TestSessionTracker:
    def test_initial_state(self, tracker):
        assert tracker.turn_count == 0
        assert tracker.last_store_turn == 0
        assert tracker.topic_freq == {}

    def test_record_call(self, tracker):
        tracker.record_call("search", topic="foo")
        assert tracker.turn_count == 1
        assert tracker.topic_freq["foo"] == 1

    def test_record_call_no_topic(self, tracker):
        tracker.record_call("search")
        assert tracker.turn_count == 1
        assert tracker.topic_freq == {}

    def test_record_store_resets_counter(self, tracker):
        for _ in range(5):
            tracker.record_call("search")
        tracker.record_store()
        assert tracker.last_store_turn == 5

    def test_reset_all(self, tracker):
        for _ in range(5):
            tracker.record_call("search", topic="foo")
        tracker.reset_all()
        assert tracker.turn_count == 0
        assert tracker.last_store_turn == 0
        assert tracker.topic_freq == {}

    def test_no_nudge_initially(self, tracker):
        assert tracker.get_nudge() is None

    def test_store_reminder_nudge(self, tracker):
        for _ in range(10):
            tracker.record_call("search")
        nudge = tracker.get_nudge()
        assert nudge is not None
        assert "REMINDER" in nudge

    def test_store_reminder_threshold_configurable(self):
        tracker = SessionTracker(NudgeConfig(store_reminder_turns=3))
        for _ in range(3):
            tracker.record_call("search")
        nudge = tracker.get_nudge()
        assert "REMINDER" in nudge

    def test_checkpoint_nudge(self, tracker):
        for _ in range(25):
            tracker.record_call("search")
        tracker.record_store()  # Reset store counter to isolate checkpoint
        nudge = tracker.get_nudge()
        assert nudge is not None
        assert "CHECKPOINT" in nudge

    def test_mandatory_stop_nudge(self, tracker):
        for _ in range(50):
            tracker.record_call("search")
        tracker.record_store()
        nudge = tracker.get_nudge()
        assert nudge is not None
        assert "MANDATORY STOP" in nudge

    def test_mandatory_stop_overrides_checkpoint(self, tracker):
        for _ in range(50):
            tracker.record_call("search")
        tracker.record_store()
        nudge = tracker.get_nudge()
        assert "MANDATORY STOP" in nudge
        assert "CHECKPOINT" not in nudge

    def test_hyper_focus_nudge(self, tracker):
        for _ in range(3):
            tracker.record_call("search", topic="same-thing")
        tracker.record_store()
        nudge = tracker.get_nudge(topic="same-thing")
        assert nudge is not None
        assert "WARNING" in nudge
        assert "same-thing" in nudge

    def test_hyper_focus_threshold_configurable(self):
        tracker = SessionTracker(NudgeConfig(hyper_focus_threshold=2))
        for _ in range(2):
            tracker.record_call("search", topic="topic")
        tracker.record_store()
        nudge = tracker.get_nudge(topic="topic")
        assert "WARNING" in nudge

    def test_rules_check_nudge(self):
        tracker = SessionTracker(NudgeConfig(
            rules_check_interval=5,
            store_reminder_turns=100,  # suppress store reminder
        ))
        for _ in range(5):
            tracker.record_call("search")
        tracker.record_store()
        nudge = tracker.get_nudge()
        assert nudge is not None
        assert "RULES CHECK" in nudge

    def test_rules_check_disabled_when_zero(self):
        tracker = SessionTracker(NudgeConfig(
            rules_check_interval=0,
            store_reminder_turns=100,
        ))
        for _ in range(15):
            tracker.record_call("search")
        tracker.record_store()
        nudge = tracker.get_nudge()
        assert nudge is None

    def test_plugin_nudge(self, tracker):
        def custom_nudge(t):
            return "CUSTOM: hello" if t.turn_count >= 2 else None

        tracker.register_plugin_nudge(custom_nudge)
        tracker.record_call("a")
        tracker.record_store()
        assert tracker.get_nudge() is None

        tracker.record_call("b")
        tracker.record_store()
        nudge = tracker.get_nudge()
        assert "CUSTOM: hello" in nudge

    def test_elapsed_seconds(self, tracker):
        # Just verify it returns a non-negative int
        assert tracker.elapsed_seconds() >= 0

    def test_blocking_disabled_by_default(self, tracker):
        """With default config, record_call never raises."""
        for _ in range(100):
            tracker.record_call("search")

    def test_blocking_enabled(self):
        """When mandatory_stop_blocking=True, calls past threshold raise."""
        cfg = NudgeConfig(
            mandatory_stop_turns=5,
            mandatory_stop_grace=2,
            mandatory_stop_blocking=True,
        )
        t = SessionTracker(cfg)
        # 7 calls should succeed (5 mandatory + 2 grace)
        for _ in range(7):
            t.record_call("search")
        # 8th call should raise
        with pytest.raises(MandatoryStopError):
            t.record_call("search")

    def test_blocking_exempt_tools(self):
        """Exempt tools (session_handoff, save_snapshot, etc.) never block."""
        cfg = NudgeConfig(
            mandatory_stop_turns=5,
            mandatory_stop_grace=2,
            mandatory_stop_blocking=True,
        )
        t = SessionTracker(cfg)
        t.turn_count = 100
        # All exempt tools should work
        for tool in SessionTracker.EXEMPT_TOOLS:
            t.record_call(tool)

    def test_blocking_clears_after_reset(self):
        """reset_all() clears turn_count, unblocking tools."""
        cfg = NudgeConfig(
            mandatory_stop_turns=5,
            mandatory_stop_grace=2,
            mandatory_stop_blocking=True,
        )
        t = SessionTracker(cfg)
        for _ in range(7):
            t.record_call("search")
        # Blocked
        with pytest.raises(MandatoryStopError):
            t.record_call("search")
        # Reset (as session_handoff does)
        t.reset_all()
        # Should work again
        t.record_call("search")
        assert t.turn_count == 1


class TestNudgeEscalation:
    """Tests for the nudge escalation system."""

    def test_no_escalation_without_nudge(self):
        """No escalation if no nudge has fired."""
        t = SessionTracker(NudgeConfig(
            store_reminder_turns=100,  # won't fire
            nudge_escalation_threshold=3,
        ))
        for _ in range(20):
            t.record_call("search")
        # Should not raise

    def test_store_reminder_escalation(self):
        """After ignoring store_reminder N times, blocks."""
        t = SessionTracker(NudgeConfig(
            store_reminder_turns=2,
            checkpoint_turns=100,
            mandatory_stop_turns=100,
            rules_check_interval=0,
            nudge_escalation_threshold=3,
        ))
        # 2 calls without store → store_reminder fires on get_nudge
        t.record_call("search")
        t.record_call("search")
        nudge = t.get_nudge()
        assert "REMINDER" in nudge
        assert "store_reminder" in t.pending_nudges

        # Ignore twice (under threshold)
        t.record_call("search")  # ignored_counts["store_reminder"] = 1
        t.get_nudge()
        t.record_call("search")  # ignored_counts["store_reminder"] = 2
        t.get_nudge()
        # 3rd ignore hits threshold → blocks on this call
        with pytest.raises(MandatoryStopError, match="ESCALATED BLOCK.*store_reminder"):
            t.record_call("search")

    def test_resolving_tool_clears_escalation(self):
        """Calling a resolving tool resets the ignored count."""
        t = SessionTracker(NudgeConfig(
            store_reminder_turns=2,
            checkpoint_turns=100,
            mandatory_stop_turns=100,
            rules_check_interval=0,
            nudge_escalation_threshold=3,
        ))
        t.record_call("search")
        t.record_call("search")
        t.get_nudge()  # fires store_reminder

        # Ignore twice
        t.record_call("search")
        t.get_nudge()
        t.record_call("search")
        assert t.ignored_counts.get("store_reminder", 0) == 2

        # Resolve with add_knowledge
        t.record_call("add_knowledge")
        assert "store_reminder" not in t.pending_nudges
        assert "store_reminder" not in t.ignored_counts

    def test_rules_check_escalation(self):
        """rules_check nudge escalates when ignored."""
        t = SessionTracker(NudgeConfig(
            store_reminder_turns=100,
            checkpoint_turns=100,
            mandatory_stop_turns=100,
            rules_check_interval=5,
            nudge_escalation_threshold=2,
        ))
        # Get to turn 5 → rules_check fires
        # Use "some_tool" to avoid resolving rules_check (search resolves it)
        for _ in range(5):
            t.record_call("some_tool")
        t.record_store()
        nudge = t.get_nudge()
        assert "RULES CHECK" in nudge

        # Ignore once with a non-resolving tool
        t.record_call("some_tool")  # ignored 1
        t.get_nudge()
        # 2nd ignore hits threshold → blocks
        with pytest.raises(MandatoryStopError, match="ESCALATED BLOCK.*rules_check"):
            t.record_call("some_tool")

    def test_rules_check_resolved_by_search(self):
        """Calling 'search' resolves rules_check."""
        t = SessionTracker(NudgeConfig(
            store_reminder_turns=100,
            checkpoint_turns=100,
            mandatory_stop_turns=100,
            rules_check_interval=5,
            nudge_escalation_threshold=3,
        ))
        for _ in range(5):
            t.record_call("search")
        t.record_store()
        t.get_nudge()  # fires rules_check

        # Ignore once
        t.record_call("some_other_tool")
        assert t.ignored_counts.get("rules_check", 0) == 1

        # Resolve with search (which is in RESOLVES["rules_check"])
        t.record_call("search")
        assert "rules_check" not in t.pending_nudges

    def test_exempt_tools_not_blocked_by_escalation(self):
        """Exempt tools bypass escalation blocks even when threshold met."""
        t = SessionTracker(NudgeConfig(
            store_reminder_turns=2,
            checkpoint_turns=100,
            mandatory_stop_turns=100,
            rules_check_interval=0,
            nudge_escalation_threshold=3,
        ))
        t.record_call("search")
        t.record_call("search")
        t.get_nudge()  # fires store_reminder

        # Ignore twice (under threshold)
        t.record_call("search")  # ignored_counts = 1
        t.get_nudge()
        t.record_call("search")  # ignored_counts = 2

        # session_handoff is exempt — increments ignored but doesn't block
        t.record_call("session_handoff")  # ignored_counts = 3 but exempt
        # Non-exempt tool should now block (count >= threshold)
        with pytest.raises(MandatoryStopError, match="ESCALATED BLOCK"):
            t.record_call("search")

    def test_reset_all_clears_escalation(self):
        """reset_all clears pending nudges and ignored counts."""
        t = SessionTracker(NudgeConfig(
            store_reminder_turns=2,
            nudge_escalation_threshold=3,
        ))
        t.record_call("search")
        t.record_call("search")
        t.get_nudge()
        t.record_call("search")  # ignored once

        assert len(t.pending_nudges) > 0
        assert len(t.ignored_counts) > 0

        t.reset_all()
        assert t.pending_nudges == set()
        assert t.ignored_counts == {}

    def test_multiple_nudge_types_independent(self):
        """Different nudge types track independently."""
        t = SessionTracker(NudgeConfig(
            store_reminder_turns=2,
            checkpoint_turns=100,
            mandatory_stop_turns=100,
            rules_check_interval=5,
            nudge_escalation_threshold=3,
        ))
        # Trigger store_reminder at turn 2
        t.record_call("search")
        t.record_call("search")
        t.get_nudge()
        assert "store_reminder" in t.pending_nudges

        # Resolve store_reminder with add_rule
        t.record_call("add_rule")
        t.record_store()
        assert "store_reminder" not in t.pending_nudges

        # Continue to turn 5 → rules_check fires
        t.record_call("search")
        t.record_call("search")
        t.get_nudge()
        assert "rules_check" in t.pending_nudges
        # store_reminder should NOT be pending (was resolved)
        assert "store_reminder" not in t.pending_nudges or t.ignored_counts.get("store_reminder", 0) == 0
