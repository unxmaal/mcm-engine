"""PostToolUseFailure hook (issue #158): failure-triggered recall.

A tool failure is a concrete retrieval anchor — no guessing whether context is
needed. On a failure, match the error against known errors/fixes in the KB (the
same corpus report_error searches) and inject the match via additionalContext.

A dedup key suppresses re-lookup for the same failure within a session: a retry
loop on one error must not fire the same search on every attempt. The key is a
hash of the error SIGNATURE (the error with run-to-run noise — hex addresses,
bare integers, absolute paths — normalized out), stored per agent in the shared
hook state.

Skips user interrupts (is_interrupt) — those are not failures to recall against.
Fail-open (RULE #61): any error -> inject nothing, exit 0.

Claude Code settings wiring (manual; there is no in-repo installer):
  "hooks": {
    "PostToolUseFailure": [
      {"hooks": [{"type": "command", "command": "mcm-engine post-tool-use-failure"}]}
    ]
  }
"""
from __future__ import annotations

import hashlib
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
# A failure lookup older than this is forgotten, so a failure recurring much
# later in a long session is surfaced again.
DEDUP_TTL_SECONDS = 6 * 3600

_HEX = re.compile(r"\b0x[0-9a-fA-F]+\b")
_NUM = re.compile(r"\b\d+\b")
_ABS_PATH = re.compile(r"(/[^\s:'\"]+)+")
_WS = re.compile(r"\s+")

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_CAMEL = re.compile(r"[a-z][A-Z]")
_QUOTED = re.compile(r"['\"`]([A-Za-z][\w.\-]{2,})['\"`]")

_STOPWORDS = frozenset({
    "error", "failed", "failure", "exception", "traceback", "the", "and", "for",
    "with", "that", "this", "from", "not", "was", "were", "are", "you", "cannot",
    "could", "would", "should", "while", "when", "none", "null", "true", "false",
    "line", "file", "call", "last", "most", "recent", "during", "object", "value",
})


def _error_text(event: dict) -> str:
    """Pull the error text out of the event, tolerant of shape differences."""
    err = event.get("error")
    if isinstance(err, str) and err.strip():
        return err
    resp = event.get("tool_response")
    if isinstance(resp, str) and resp.strip():
        return resp
    if isinstance(resp, dict):
        for k in ("error", "stderr", "message", "output"):
            v = resp.get(k)
            if isinstance(v, str) and v.strip():
                return v
    if isinstance(err, dict):
        for k in ("message", "error"):
            v = err.get(k)
            if isinstance(v, str) and v.strip():
                return v
    return ""


def signature(error_text: str, tool_name: str = "") -> str:
    """Stable skeleton of a failure: the error with run-to-run noise removed, so
    the same failure class dedups across retries but different errors do not."""
    s = error_text
    s = _ABS_PATH.sub(lambda m: "/" + m.group(0).rsplit("/", 1)[-1], s)
    s = _HEX.sub("0xADDR", s)
    s = _NUM.sub("N", s)
    s = _WS.sub(" ", s).strip().lower()
    return f"{tool_name.lower()}|{s[:400]}"


def _dedup_key(error_text: str, tool_name: str) -> str:
    return hashlib.sha1(signature(error_text, tool_name).encode("utf-8")).hexdigest()[:16]


def query_from_error(error_text: str) -> str:
    """One most-distinctive token for an ANDed FTS5 query: a quoted identifier
    (a missing module/symbol), else a CamelCase exception class, else the
    longest identifier-shaped or non-stopword token. "" if nothing usable."""
    quoted = _QUOTED.findall(error_text)
    if quoted:
        return max(quoted, key=len).lower()

    words = _WORD_RE.findall(error_text)
    camel = [w for w in words if _CAMEL.search(w)]
    if camel:
        return max(camel, key=len).lower()

    ident = [w for w in words if "_" in w or any(c.isdigit() for c in w)]
    if ident:
        return max(ident, key=len).lower()

    candidates = [w for w in words if len(w) >= 4 and w.lower() not in _STOPWORDS]
    if not candidates:
        return ""
    return max(candidates, key=len).lower()


def _http_search(query: str, cwd: Path) -> str:
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


def _seen_before(event: dict, key: str, cwd: Path, *, now: float) -> bool:
    """True if this failure signature was already looked up this session. Also
    records the key (and the query topic as covered). Fail-open: on any error,
    returns False so the lookup still proceeds."""
    try:
        sp = _state_path(cwd)
        state = _read_state(sp)
        _prune_stale(state, now=now)
        entry = state.setdefault(_agent_key(event), {"last_reset_at": now})
        lookups = entry.setdefault("failure_lookups", {})
        # prune stale dedup keys
        for k in [k for k, t in lookups.items() if now - t > DEDUP_TTL_SECONDS]:
            del lookups[k]
        seen = key in lookups
        lookups[key] = now
        _write_state(sp, state)
        return seen
    except Exception:
        return False


def _record_coverage(event: dict, query: str, cwd: Path, *, now: float) -> None:
    try:
        sp = _state_path(cwd)
        state = _read_state(sp)
        entry = state.setdefault(_agent_key(event), {"last_reset_at": now})
        coverage.record_topic(entry, query, now=now)
        coverage.prune_topics(entry, now=now)
        _write_state(sp, state)
    except Exception:
        pass


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

    if event.get("is_interrupt"):
        return 0  # a user interrupt is not a failure to recall against

    error_text = _error_text(event)
    if not error_text:
        return 0

    tool_name = event.get("tool_name", "") or ""
    cwd = Path(event.get("cwd") or os.getcwd())
    now = time.time()

    key = _dedup_key(error_text, tool_name)
    if _seen_before(event, key, cwd, now=now):
        return 0  # already looked this failure up this session

    query = query_from_error(error_text)
    if not query:
        return 0

    retriever = search or _http_search
    try:
        result = retriever(query, cwd)
    except Exception:
        result = ""

    _record_coverage(event, query, cwd, now=now)

    if not result:
        return 0

    context = (
        f"[mcm-engine] This failure may match known errors/fixes; auto-retrieved "
        f"for '{query}' (verify before relying on it):\n{result}"
    )
    try:
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PostToolUseFailure",
                "additionalContext": context,
            }
        }))
    except Exception:
        return 0
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
