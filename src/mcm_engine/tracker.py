"""SessionTracker — behavioral nudges to prevent context exhaustion."""
from __future__ import annotations

import time

from .config import NudgeConfig


class MandatoryStopError(Exception):
    """Raised when a tool is blocked due to mandatory stop enforcement."""
    pass


class SessionTracker:
    """Tracks tool call frequency and generates behavioral nudges.

    Nudge types (in priority order):
    1. Mandatory stop — too many tool calls without handoff
    2. Checkpoint — long session, should snapshot
    3. Store reminder — many calls without storing knowledge
    4. Hyper-focus — querying same topic repeatedly
    5. Rules check — periodic re-orientation reminder

    Nudge escalation: When a nudge fires and the agent ignores it
    (doesn't call a resolving tool), the ignored count for that nudge
    type increments. After ``nudge_escalation_threshold`` ignores of
    the same type, the next tool call raises MandatoryStopError.
    """

    # Which tools resolve which nudge types.
    RESOLVES: dict[str, frozenset[str]] = {
        "store_reminder": frozenset({
            "add_rule", "add_knowledge", "add_negative",
            "report_error", "report_finding",
        }),
        "checkpoint": frozenset({
            "session_handoff", "save_snapshot",
        }),
        "mandatory_stop": frozenset({
            "session_handoff", "save_snapshot",
        }),
        "rules_check": frozenset({
            "search", "report_error", "check_compat", "read_rule",
        }),
        "hyper_focus": frozenset({
            "add_knowledge", "add_rule", "session_handoff",
        }),
    }

    # Targeted guidance for per-tool deficit nudges. Generic fallback below.
    PERIODIC_HINTS: dict[str, str] = {
        "link_knowledge": (
            "Connect related items you've stored — `link_knowledge(source, target)` "
            "builds the graph `get_related` traverses. The KB has many entries and "
            "almost no links."
        ),
        "add_negative": (
            "Hit a dead end or anti-pattern? `add_negative` records it so it's never "
            "repeated. If nothing actually failed, store a finding with `add_knowledge` "
            "instead."
        ),
    }

    def __init__(self, config: NudgeConfig | None = None) -> None:
        self.config = config or NudgeConfig()
        self.turn_count: int = 0
        self.last_store_turn: int = 0
        self.last_checkpoint_turn: int = 0
        self.topic_freq: dict[str, int] = {}
        self.session_start: float = time.time()
        self._plugin_nudge_fns: list = []
        # Nudge escalation state
        self.pending_nudges: set[str] = set()
        self.ignored_counts: dict[str, int] = {}
        # Per-tool deficit counters. calls_since[tool] counts tool calls since
        # that specific tool last fired; it resets to 0 when the tool fires.
        self.calls_since: dict[str, int] = {
            tool: 0 for tool in self.config.periodic_tools
        }
        # Every tool called at least once this session — used by the
        # session-end gate to surface what was never touched.
        self.called_this_session: set[str] = set()

    def record_call(self, tool_name: str, topic: str | None = None) -> None:
        """Record a tool invocation. Call this at the start of every tool.

        Raises MandatoryStopError if:
        1. Blocking is enabled and we're past mandatory_stop_turns + grace, OR
        2. A nudge type has been ignored >= nudge_escalation_threshold times.

        Exempt tools (session_handoff, save_snapshot, etc.) are never blocked.
        """
        self.turn_count += 1
        if topic:
            key = topic.lower().strip()
            self.topic_freq[key] = self.topic_freq.get(key, 0) + 1

        self.called_this_session.add(tool_name)
        # Per-tool deficit counters: every tracked tool's counter advances by
        # one; the one that just fired resets to zero.
        for t in self.calls_since:
            self.calls_since[t] += 1
        if tool_name in self.calls_since:
            self.calls_since[tool_name] = 0

        # Check which pending nudges this tool resolves
        resolved = set()
        for nudge_type in list(self.pending_nudges):
            if tool_name in self._resolving_tools(nudge_type):
                resolved.add(nudge_type)
        # Clear resolved nudges and their ignored counts
        for nudge_type in resolved:
            self.pending_nudges.discard(nudge_type)
            self.ignored_counts.pop(nudge_type, None)
        # Unresolved pending nudges: increment ignored count
        for nudge_type in list(self.pending_nudges - resolved):
            self.ignored_counts[nudge_type] = self.ignored_counts.get(nudge_type, 0) + 1

        if self.is_blocked(tool_name):
            raise MandatoryStopError(
                f"BLOCKED: {self.turn_count} tool calls without checkpoint. "
                "You MUST call `session_handoff` or `save_snapshot` before "
                "any other tool. This block resets after checkpointing."
            )

        # Nudge escalation: block if any nudge type exceeded threshold
        if tool_name not in self.EXEMPT_TOOLS:
            threshold = self.config.nudge_escalation_threshold
            for nudge_type, count in self.ignored_counts.items():
                if count >= threshold:
                    resolving = self._resolving_tools(nudge_type)
                    raise MandatoryStopError(
                        f"ESCALATED BLOCK: '{nudge_type}' nudge ignored {count} times. "
                        f"You MUST call one of: {', '.join(sorted(resolving))} "
                        "before any other tool."
                    )

    def _resolving_tools(self, nudge_type: str) -> frozenset[str]:
        """Tools that clear a nudge. A periodic nudge (``periodic:<tool>``) is
        cleared only by that exact tool; all others use the RESOLVES table."""
        if nudge_type.startswith("periodic:"):
            return frozenset({nudge_type.split(":", 1)[1]})
        return self.RESOLVES.get(nudge_type, frozenset())

    def record_store(self) -> None:
        """Record that knowledge was stored. Resets the store reminder counter."""
        self.last_store_turn = self.turn_count

    def reset_all(self) -> None:
        """Reset all counters. Called on session_handoff."""
        self.turn_count = 0
        self.last_store_turn = 0
        self.last_checkpoint_turn = 0
        self.topic_freq.clear()
        self.pending_nudges.clear()
        self.ignored_counts.clear()
        for t in self.calls_since:
            self.calls_since[t] = 0
        self.called_this_session.clear()

    def elapsed_seconds(self) -> int:
        """Seconds since session start."""
        return int(time.time() - self.session_start)

    def register_plugin_nudge(self, fn) -> None:
        """Register a plugin nudge function: fn(tracker) -> str | None."""
        self._plugin_nudge_fns.append(fn)

    # Tool names that are exempt from blocking (must always work).
    EXEMPT_TOOLS = frozenset({
        "session_handoff", "save_snapshot", "session_start", "session_summary",
    })

    def is_blocked(self, tool_name: str = "") -> bool:
        """Return True if mandatory_stop_blocking is enabled and we're past the grace period.

        Exempt tools (session_handoff, save_snapshot, etc.) are never blocked.
        Uses turns since last checkpoint, not total turns.
        """
        if tool_name in self.EXEMPT_TOOLS:
            return False
        cfg = self.config
        if not cfg.mandatory_stop_blocking:
            return False
        turns_since_checkpoint = self.turn_count - self.last_checkpoint_turn
        return turns_since_checkpoint > cfg.mandatory_stop_turns + cfg.mandatory_stop_grace

    def get_nudge(self, topic: str | None = None) -> str | None:
        """Generate a behavioral nudge based on current state, or None.

        Each fired nudge is tracked in ``pending_nudges`` so that
        ``record_call`` can detect when a nudge is ignored (the agent
        calls a non-resolving tool after a nudge fires).
        """
        messages: list[str] = []
        fired: list[str] = []
        turns_since_store = self.turn_count - self.last_store_turn
        turns_since_checkpoint = self.turn_count - self.last_checkpoint_turn
        cfg = self.config

        # Mandatory stop (based on turns since last checkpoint, not total)
        if turns_since_checkpoint >= cfg.mandatory_stop_turns:
            messages.append(
                f"MANDATORY STOP: {turns_since_checkpoint} tool calls since last checkpoint. "
                "You MUST call `session_handoff` NOW before continuing. Do NOT say "
                "'but first let me...' — that is the failure mode this "
                "checkpoint prevents."
            )
            fired.append("mandatory_stop")
        # Checkpoint (based on turns since last checkpoint)
        elif turns_since_checkpoint >= cfg.checkpoint_turns:
            messages.append(
                f"CHECKPOINT: {turns_since_checkpoint} tool calls since last checkpoint. "
                "Call `session_handoff` or `save_snapshot` to reset. "
                "If debugging the same issue for >3 attempts, delegate "
                "to a sub-agent."
            )
            fired.append("checkpoint")

        # Store reminder
        if turns_since_store >= cfg.store_reminder_turns:
            messages.append(
                f"REMINDER: You've made {turns_since_store} tool calls "
                "without storing findings. Use `add_knowledge` or "
                "`add_negative` to externalize what you've learned "
                "before it compacts out of context."
            )
            fired.append("store_reminder")

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
                fired.append("hyper_focus")

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
            fired.append("rules_check")

        # Per-tool deficit nudges — each names the specific missing tool so the
        # agent can't satisfy it by calling some other store tool.
        for tool, threshold in self.config.periodic_tools.items():
            if self.calls_since.get(tool, 0) >= threshold:
                hint = self.PERIODIC_HINTS.get(
                    tool, f"Call `{tool}` to reset this counter."
                )
                messages.append(
                    f"PERIODIC: {self.calls_since[tool]} tool calls without "
                    f"`{tool}`. {hint}"
                )
                fired.append(f"periodic:{tool}")

        # Plugin nudges
        for fn in self._plugin_nudge_fns:
            try:
                nudge = fn(self)
                if nudge:
                    messages.append(nudge)
            except Exception:
                pass

        # Track fired nudges for escalation
        self.pending_nudges.update(fired)

        return "\n\n".join(messages) if messages else None
