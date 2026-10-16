"""Build a replay trace from a Claude Code transcript (issue #160).

A transcript is a JSONL session log: assistant records carry `tool_use` content
blocks (the tool calls), and the following user records carry `tool_result`
blocks (with `is_error`). This reads that into an ordered list of TraceEvent,
reusing the #159 consequential-op classifier so replay sees the same op types
the live gate does.

kb_has_hit is left unknown (None): the transcript does not record whether the KB
held a relevant entry. The failure signal (`led_to_failure`, from a tool_result
`is_error`) is the label that does not need an oracle, and is what the replay
leans on until a KB oracle is wired. Tolerant of format drift: unparseable lines
and records are skipped, never raised.
"""
from __future__ import annotations

import json
from pathlib import Path

from ..hooks.mcp_enforcement import _CLAUDE_MCP_TOOL_RE, _consequential_op
from .replay import TraceEvent


def _is_lookup(tool_name: str) -> bool:
    m = _CLAUDE_MCP_TOOL_RE.match(tool_name or "")
    suffix = m.group(2) if m else (tool_name or "")
    return suffix in ("search", "report_error")


def _content_blocks(record: dict) -> list:
    msg = record.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
    else:
        content = record.get("content")
    if isinstance(content, list):
        return content
    return []


def load_transcript(path: str | Path) -> list[TraceEvent]:
    """Parse a transcript JSONL file into an ordered trace."""
    records: list[dict] = []
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            records.append(rec)

    # Pass 1: which tool_use ids ended in an error.
    errored: dict[str, bool] = {}
    for rec in records:
        for block in _content_blocks(rec):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                tid = block.get("tool_use_id")
                if tid:
                    errored[tid] = bool(block.get("is_error"))

    # Pass 2: one TraceEvent per tool_use, in file order.
    trace: list[TraceEvent] = []
    seq = 0
    for rec in records:
        for block in _content_blocks(rec):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool = block.get("name", "") or ""
            tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
            tid = block.get("id")
            if _is_lookup(tool):
                topic = (tool_input.get("query") or tool_input.get("error_text") or "")
                ev = TraceEvent(seq=seq, tool=tool, looked_up=True,
                                topic=str(topic).lower().strip())
            else:
                conseq, topic = _consequential_op(tool, {"tool_input": tool_input})
                ev = TraceEvent(seq=seq, tool=tool, consequential=conseq, topic=topic,
                                led_to_failure=bool(errored.get(tid, False)))
            trace.append(ev)
            seq += 1
    return trace
