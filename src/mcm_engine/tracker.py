"""SessionTracker — behavioral nudges to prevent context exhaustion."""
from __future__ import annotations

import time

from .config import NudgeConfig


class SessionTracker:
    """Tracks tool call frequency and generates behavioral nudges.

    Nudge types (in priority order):
    1. Mandatory stop — too many tool calls without handoff
    2. Checkpoint — long session, should snapshot
    3. Store reminder — many calls without storing knowledge
    4. Hyper-focus — querying same topic repeatedly
    5. Rules check — periodic re-orientation reminder
    """

    def __init__(self, config: NudgeConfig | None = None) -> None:
        self.config = config or NudgeConfig()
        self.turn_count: int = 0
        self.last_store_turn: int = 0
        self.topic_freq: dict[str, int] = {}
        self.session_start: float = time.time()
        self._plugin_nudge_fns: list = []

    def record_call(self, tool_name: str, topic: str | None = None) -> None:
        """Record a tool invocation. Call this at the start of every tool."""
        self.turn_count += 1
        if topic:
            key = topic.lower().strip()
            self.topic_freq[key] = self.topic_freq.get(key, 0) + 1

    def record_store(self) -> None:
        """Record that knowledge was stored. Resets the store reminder counter."""
        self.last_store_turn = self.turn_count

    def reset_all(self) -> None:
        """Reset all counters. Called on session_handoff."""
        self.last_store_turn = self.turn_count
        self.topic_freq.clear()

    def elapsed_seconds(self) -> int:
        """Seconds since session start."""
        return int(time.time() - self.session_start)

    def register_plugin_nudge(self, fn) -> None:
        """Register a plugin nudge function: fn(tracker) -> str | None."""
        self._plugin_nudge_fns.append(fn)

    def get_nudge(self, topic: str | None = None) -> str | None:
        """Generate a behavioral nudge based on current state, or None."""
        messages: list[str] = []
        turns_since_store = self.turn_count - self.last_store_turn
        cfg = self.config

        # Mandatory stop
        if self.turn_count >= cfg.mandatory_stop_turns:
            messages.append(
                f"MANDATORY STOP: {self.turn_count} tool calls. You MUST call "
                "`session_handoff` NOW before continuing. Do NOT say "
                "'but first let me...' — that is the failure mode this "
                "checkpoint prevents."
            )
        # Checkpoint
        elif self.turn_count >= cfg.checkpoint_turns:
            messages.append(
                f"CHECKPOINT: {self.turn_count} tool calls this session. "
                "Call `session_handoff` to snapshot your current state. "
                "If debugging the same issue for >3 attempts, delegate "
                "to a sub-agent."
            )

        # Store reminder
        if turns_since_store >= cfg.store_reminder_turns:
            messages.append(
                f"REMINDER: You've made {turns_since_store} tool calls "
                "without storing findings. Use `add_knowledge` or "
                "`add_negative` to externalize what you've learned "
                "before it compacts out of context."
            )

        # Hyper-focus detection
        if topic:
            key = topic.lower().strip()
            freq = self.topic_freq.get(key, 0)
            if freq >= cfg.hyper_focus_threshold:
                messages.append(
                    f"WARNING: You've queried '{topic}' {freq} times. "
                    "Either store your findings and move on, or delegate "
                    "to a sub-agent with Task()."
                )

        # Rules check
        if (
            cfg.rules_check_interval > 0
            and self.turn_count > 0
            and self.turn_count % cfg.rules_check_interval == 0
        ):
            messages.append(
                "RULES CHECK: (1) Are you following project instructions? "
                "(2) Have you stored findings? "
                "(3) Are you hyper-focused on one approach? "
                "(4) Should you delegate?"
            )

        # Plugin nudges
        for fn in self._plugin_nudge_fns:
            try:
                nudge = fn(self)
                if nudge:
                    messages.append(nudge)
            except Exception:
                pass

        return "\n\n".join(messages) if messages else None
