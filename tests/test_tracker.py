"""Tests for SessionTracker — nudge thresholds, topic frequency."""
import pytest

from mcm_engine.config import NudgeConfig
from mcm_engine.tracker import SessionTracker


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
        assert tracker.last_store_turn == 5
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
