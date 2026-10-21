"""UserPromptSubmit hook (issue #157): task-aware retrieval + inject.

When a new prompt references history (prior decisions, prior failures,
established preferences, ongoing work), search the KB and inject the result via
hookSpecificOutput.additionalContext BEFORE the model reasons, and record the
topic as covered (hooks/coverage.py). A prompt that references no history is a
fast no-op — the classifier returns early before any search.

Deterministic classifier only. No LLM: mcm runs model-agnostic behind a LiteLLM
gateway (DECISION #39), so an LLM-evaluated hook would be a gateway round-trip
on the critical path, and this hook blocks prompt processing until it returns.

Fail-open (RULE #61): any error -> inject nothing, exit 0. Never trap the agent.

Claude Code settings wiring (manual; there is no in-repo installer):
  "hooks": {
    "UserPromptSubmit": [
      {"hooks": [{"type": "command", "command": "mcm-engine user-prompt-submit"}]}
    ]
  }
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional

from . import coverage
from .mcp_enforcement import (
    _agent_key,
    _mcp_http_endpoint,
    _prune_stale,
    _read_state,
    _state_path,
    _write_state,
    mcp_http_call_tool,
)

SEARCH_TIMEOUT_S = 1.0
SEARCH_LIMIT = 3

# Issue #172: a hard ceiling on the injected additionalContext. Even with the
# per-hit caps (#171), a prompt naming several identifiers can pull enough to
# bloat the turn (observed 11-12 KB injections, larger than CLAUDE.md, which
# Claude Code persisted as "Output too large"). Truncate at a hit boundary and
# point at the live tool. 0 disables.
INJECT_MAX_BYTES = max(0, int(os.environ.get("MCM_INJECT_MAX_BYTES", "2048")))


def _truncate_injection(result: str, query: str) -> str:
    """Bound the injected search result to INJECT_MAX_BYTES, cutting at a hit
    boundary (hits are joined by a blank line) so a hit is never shown half,
    and appending a pointer to the live tool."""
    if INJECT_MAX_BYTES <= 0 or len(result.encode("utf-8")) <= INJECT_MAX_BYTES:
        return result
    pointer = f"\n\n(truncated — run search('{query}') for the rest)"
    budget = max(0, INJECT_MAX_BYTES - len(pointer.encode("utf-8")))
    hits = result.split("\n\n")
    kept: list[str] = []
    used = 0
    for hit in hits:
        add = len(hit.encode("utf-8")) + (2 if kept else 0)
        if used + add > budget:
            break
        kept.append(hit)
        used += add
    if not kept:
        # A single hit already blows the budget: hard-cut its bytes.
        kept = [result.encode("utf-8")[:budget].decode("utf-8", "ignore")]
    return "\n\n".join(kept) + pointer

# Phrases that signal the prompt leans on something from before this turn:
# prior decisions, prior failures, established preferences, or ongoing work.
# Matched on word boundaries (so "still" does not fire inside "distill").
_HISTORY_SIGNALS = (
    r"again", r"last time", r"earlier", r"previously", r"before",
    r"we (?:decided|agreed|chose|said|discussed|did|had|use|used)",
    r"you (?:said|told|mentioned|suggested|recommended)",
    r"remember", r"recall", r"as (?:before|usual|discussed|decided)",
    r"the usual", r"like (?:before|last time)", r"revert", r"undo",
    r"same (?:error|bug|issue|problem|thing)", r"that (?:error|bug|issue)",
    r"continue", r"resume", r"where we left off", r"still (?:failing|broken)",
    r"the plan", r"our (?:convention|standard|policy)", r"why did we",
    r"how did we", r"keep (?:getting|hitting)", r"known (?:issue|bug)",
)
_HISTORY_RE = re.compile(r"\b(?:" + "|".join(_HISTORY_SIGNALS) + r")\b", re.I)

# Tokens too common to be a useful FTS query on their own.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "that", "this", "from", "have", "has", "had",
    "was", "were", "are", "you", "your", "our", "its", "it's", "they", "them",
    "then", "than", "when", "what", "why", "how", "did", "does", "doing", "done",
    "can", "could", "should", "would", "will", "shall", "may", "might", "must",
    "about", "into", "onto", "over", "under", "again", "still", "same", "like",
    "last", "time", "before", "after", "earlier", "previously", "remember",
    "recall", "continue", "resume", "revert", "undo", "please", "let", "lets",
    "want", "need", "make", "made", "get", "got", "set", "use", "used", "using",
    "work", "working", "thing", "things", "stuff", "some", "any", "all", "one",
    "two", "now", "here", "there", "which", "where", "while", "been", "being",
})

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def _pick_keyword(prompt: str) -> str:
    """Pick the single most distinctive token for an ANDed FTS5 query.

    One token, not a sentence: search ANDs every token, so a phrase usually
    returns nothing. Prefer an identifier-shaped token (snake_case, has a digit,
    or interior capital — these name project artifacts); otherwise the longest
    non-stopword word of 4+ chars. Returns "" when nothing usable is found, so
    the caller skips the search rather than fire a hopeless one."""
    words = _WORD_RE.findall(prompt)

    def is_identifier(w: str) -> bool:
        return "_" in w or any(c.isdigit() for c in w) or bool(re.search(r"[a-z][A-Z]", w))

    identifiers = [w for w in words if is_identifier(w)]
    if identifiers:
        return max(identifiers, key=len).lower()

    candidates = [w for w in words if len(w) >= 4 and w.lower() not in _STOPWORDS]
    if not candidates:
        return ""
    return max(candidates, key=len).lower()


def classify(prompt: str) -> tuple[bool, str]:
    """Return (is_historical, query). query is "" when the prompt is not
    historical or no usable keyword could be extracted (either way: no search)."""
    if not prompt or not _HISTORY_RE.search(prompt):
        return (False, "")
    return (True, _pick_keyword(prompt))


def _http_search(query: str, cwd: Path) -> str:
    """Search the KB over MCP/HTTP, bounded and fail-open. Returns "" on any
    failure or when no HTTP server is configured (stdio dev has no injection;
    the in-process nudge system covers that path)."""
    try:
        url, headers = _mcp_http_endpoint(cwd)
        if not url:
            return ""
        return mcp_http_call_tool(
            url, "search",
            {"query": query, "scope": "all", "limit": SEARCH_LIMIT, "detail": "summary"},
            headers=headers, timeout=SEARCH_TIMEOUT_S,
        )
    except Exception:
        return ""


def _record_coverage(event: dict, query: str, cwd: Path) -> None:
    """Record the retrieved topic in the shared hook state, fail-open."""
    try:
        now = time.time()
        sp = _state_path(cwd)
        state = _read_state(sp)
        _prune_stale(state, now=now)
        # last_reset_at keeps a coverage-only entry (created before the agent's
        # first tool call) from being pruned and lets the enforcement hook grow
        # its own counters onto the same entry via setdefault.
        entry = state.setdefault(_agent_key(event), {"last_reset_at": now})
        coverage.record_topic(entry, query, now=now)
        coverage.prune_topics(entry, now=now)
        _write_state(sp, state)
    except Exception:
        pass


def _claim_injection(event: dict, query: str, cwd: Path) -> bool:
    """Issue #172 dedup: return True if this query was already INJECTED earlier
    in the session (so the caller skips a repeat injection), else mark it as
    injected and return False. Tracked separately from coverage, which records
    even an empty retrieval — this marks only material actually shown to the
    model. Fail-open: on any error, allow the injection (return False)."""
    key = (query or "").lower().strip()
    if not key:
        return False
    try:
        now = time.time()
        sp = _state_path(cwd)
        state = _read_state(sp)
        entry = state.setdefault(_agent_key(event), {"last_reset_at": now})
        injected = entry.setdefault("injected_topics", {})
        # Drop stale marks so the map does not grow without bound.
        for k in [k for k, ts in injected.items()
                  if now - ts > coverage.COVERAGE_TTL_SECONDS]:
            del injected[k]
        seen = injected.get(key)
        if seen is not None and now - seen <= coverage.COVERAGE_TTL_SECONDS:
            return True
        injected[key] = now
        _write_state(sp, state)
        return False
    except Exception:
        return False


def main(
    argv: Optional[list[str]] = None,
    *,
    search: Optional[Callable[[str, Path], str]] = None,
) -> int:
    raw = sys.stdin.read()
    try:
        event = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return 0

    prompt = event.get("prompt", "") or ""
    cwd_raw = event.get("cwd") or os.getcwd()
    cwd = Path(cwd_raw)

    is_historical, query = classify(prompt)
    if not is_historical or not query:
        return 0  # fast path: nothing to inject

    retriever = search or _http_search
    try:
        result = retriever(query, cwd)
    except Exception:
        result = ""

    # Coverage is recorded whenever the hook retrieved for this topic, even if
    # the search came back empty: the agent's attention was directed at it.
    _record_coverage(event, query, cwd)

    if not result:
        return 0

    # Issue #172: inject a given topic's material at most once per session — a
    # generic NEGATIVE should reach the model once, not on every later prompt
    # that happens to name the same identifier.
    if _claim_injection(event, query, cwd):
        return 0

    result = _truncate_injection(result, query)
    context = (
        f"[mcm-engine] This request references prior work; auto-retrieved for "
        f"'{query}' (verify relevance before relying on it):\n{result}"
    )
    try:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context,
            }
        }))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
