"""SessionTracker — behavioral nudges to prevent context exhaustion."""
from __future__ import annotations

import threading
import time
from weakref import WeakKeyDictionary

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

    # Read-only query tools (issue #83 secondary hardening). These surface
    # existing knowledge; they are neither a mutation (nothing to store) nor a
    # checkpoint-worthy step. They advance turn_count (telemetry, hyper-focus,
    # rules-check) but MUST NOT advance the write-hygiene deficits
    # (store_reminder / checkpoint / mandatory_stop) or accrue nudge-ignore
    # counts — otherwise a long read burst (e.g. one `ingest --remote` run's many
    # `sift_candidates` batches) self-escalates and blocks the caller's own
    # follow-up write. `search` is deliberately NOT here: it resolves rules_check
    # and is part of the look-first contract.
    READ_ONLY_TOOLS: frozenset[str] = frozenset({
        "sift_candidates", "find_duplicate_rules", "find_conflicting_rules",
        "consolidation_report", "list_rules", "get_related",
    })

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
        is_read_only = tool_name in self.READ_ONLY_TOOLS

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
        # Unresolved pending nudges: increment ignored count — but a read-only
        # query is not "ignoring" a write-hygiene nudge, so it never accrues one.
        if not is_read_only:
            for nudge_type in list(self.pending_nudges - resolved):
                self.ignored_counts[nudge_type] = self.ignored_counts.get(nudge_type, 0) + 1

        if is_read_only:
            # Keep the write-hygiene deltas flat across this call (turn_count went
            # up, so bump both watermarks in lockstep) and never block on a read.
            # A subsequent WRITE tool still sees the real, preserved deficit.
            self.last_store_turn += 1
            self.last_checkpoint_turn += 1
            return

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
                    if self._is_advisory(nudge_type):
                        continue
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

    def _is_advisory(self, nudge_type: str) -> bool:
        """True for periodic nudges whose tool is advisory-only — they fire
        and accrue ignores but never escalate to a block."""
        if nudge_type.startswith("periodic:"):
            tool = nudge_type.split(":", 1)[1]
            return tool in self.config.advisory_periodic_tools
        return False

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


class ScopedTracker:
    """Per-session facade over ``SessionTracker`` (issue #83).

    One ``SessionTracker`` is process-global by construction, but the server
    process serves MANY client sessions. Sharing one tracker collapses every
    client's governance state onto a single set of counters, so one client's
    tool calls advance the nudge/escalation state that then blocks another
    client (and a ``session_handoff`` from one wipes the others' counters).

    This facade presents the exact same surface as ``SessionTracker`` but routes
    every call/attribute to a per-session tracker resolved from a caller-supplied
    ``key_provider``. Trackers are keyed in a ``WeakKeyDictionary`` on the session
    object (the FastMCP ``ServerSession`` — one stable, weak-referenceable object
    per connection, per the SDK), so they evict automatically when the session
    ends; no manual TTL. When ``key_provider`` yields ``None`` (stdio outside a
    request, tests, startup) a single shared default tracker is used.

    ``key_provider`` is injected (not hard-wired to the MCP SDK) so this module
    stays dependency-light and unit-testable: the server passes a closure that
    reads the FastMCP request context; tests pass a controllable one.
    """

    def __init__(self, config: NudgeConfig | None = None, *, key_provider=None) -> None:
        self._config = config
        self._key_provider = key_provider or (lambda: None)
        self._trackers: "WeakKeyDictionary[object, SessionTracker]" = WeakKeyDictionary()
        self._default_tracker: SessionTracker | None = None
        self._plugin_nudge_fns: list = []
        self._lock = threading.RLock()

    def _new_tracker(self) -> SessionTracker:
        t = SessionTracker(self._config)
        for fn in self._plugin_nudge_fns:
            t.register_plugin_nudge(fn)
        return t

    def _key(self):
        try:
            return self._key_provider()
        except Exception:
            return None

    def _current(self) -> SessionTracker:
        key = self._key()
        with self._lock:
            if key is None:
                if self._default_tracker is None:
                    self._default_tracker = self._new_tracker()
                return self._default_tracker
            t = self._trackers.get(key)
            if t is None:
                t = self._new_tracker()
                self._trackers[key] = t
            return t

    def register_plugin_nudge(self, fn) -> None:
        """Register a plugin nudge for EVERY session — the ones that already
        exist and any created later (fns are replayed into each new tracker)."""
        with self._lock:
            self._plugin_nudge_fns.append(fn)
            for t in list(self._trackers.values()):
                t.register_plugin_nudge(fn)
            if self._default_tracker is not None:
                self._default_tracker.register_plugin_nudge(fn)

    # --- delegate the mutating surface to the current session's tracker ---
    def record_call(self, *args, **kwargs):
        return self._current().record_call(*args, **kwargs)

    def record_store(self, *args, **kwargs):
        return self._current().record_store(*args, **kwargs)

    def get_nudge(self, *args, **kwargs):
        return self._current().get_nudge(*args, **kwargs)

    def reset_all(self, *args, **kwargs):
        return self._current().reset_all(*args, **kwargs)

    def is_blocked(self, *args, **kwargs):
        return self._current().is_blocked(*args, **kwargs)

    def elapsed_seconds(self, *args, **kwargs):
        return self._current().elapsed_seconds(*args, **kwargs)

    # --- delegate attribute reads/writes (turn_count, last_checkpoint_turn,
    #     topic_freq, config, ...) to the current session's tracker ---
    def __getattr__(self, name):
        # Only reached for names not found normally. Internal names must never
        # recurse here (they're all set in __init__).
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._current(), name)

    def __setattr__(self, name, value):
        if name.startswith("_"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._current(), name, value)
