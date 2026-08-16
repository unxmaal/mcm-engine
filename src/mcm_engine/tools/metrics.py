"""Session efficiency metrics tools (Phase 2c).

`record_session_metrics` is the write the SessionEnd hook calls (upsert, keyed on
the Claude Code session id). `session_metrics_report` is the read-only retro
surface. DIAGNOSTIC ONLY — this data is context for a human/agent retro, never an
optimization target (Goodhart). Real token counts come from the transcript and
are distinct from the token_ledger (a chars/4 KB-value heuristic).
"""
from __future__ import annotations

from statistics import median

from mcp.server.fastmcp import FastMCP

from ..backends import SessionMetricsRow
from ..tracker import SessionTracker
from ..wiring import coerce_context


def _with_nudge(result: str, tracker: SessionTracker, topic: str | None = None) -> str:
    nudge = tracker.get_nudge(topic)
    if nudge:
        return f"{result}\n\n---\n{nudge}"
    return result


def register_metrics_tools(
    mcp: FastMCP,
    ctx_or_db,
    tracker: SessionTracker,
    project_name: str = "",
) -> None:
    """Register record_session_metrics + session_metrics_report."""
    ctx = coerce_context(ctx_or_db)
    storage = ctx.storage

    @mcp.tool()
    def record_session_metrics(
        cc_session_id: str,
        first_seen_at: str = "",
        in_tokens: int = 0,
        out_tokens: int = 0,
        cache_read_tokens: int = 0,
        cache_write_tokens: int = 0,
        loc_added: int = 0,
        loc_removed: int = 0,
        loc_churned: int = 0,
        comment_lines_added: int = 0,
        code_lines_added: int = 0,
        edit_cycles_max: int = 0,
        fixation_events: int = 0,
        tool_failures: int = 0,
        project: str = "",
        extras_json: str = "",
    ) -> str:
        """Upsert one per-session efficiency record, keyed on cc_session_id (the
        Claude Code session id). Written by the SessionEnd hook; a session ends
        once but the write may be retried, so this is last-write-wins.

        DIAGNOSTIC ONLY — never an optimization target. `ended_at` defaults to
        now server-side. Token counts are the real transcript totals, distinct
        from the token_ledger heuristic.
        """
        tracker.record_call("record_session_metrics")
        if not cc_session_id:
            return _with_nudge(
                "record_session_metrics: cc_session_id is required.", tracker)
        storage.upsert_session_metrics(SessionMetricsRow(
            id=0,
            cc_session_id=cc_session_id,
            project=(project or project_name or None),
            first_seen_at=first_seen_at or None,
            ended_at=None,  # storage stamps now() when None
            in_tokens=in_tokens,
            out_tokens=out_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
            loc_added=loc_added,
            loc_removed=loc_removed,
            loc_churned=loc_churned,
            comment_lines_added=comment_lines_added,
            code_lines_added=code_lines_added,
            edit_cycles_max=edit_cycles_max,
            fixation_events=fixation_events,
            tool_failures=tool_failures,
            extras_json=extras_json or None,
        ))
        return _with_nudge(
            f"Recorded session_metrics for {cc_session_id}: {out_tokens} "
            f"out-tokens, +{loc_added}/-{loc_removed} loc (churn {loc_churned}), "
            f"fixations {fixation_events}.",
            tracker,
        )

    @mcp.tool()
    def session_metrics_report(after_id: int = 0, limit: int = 20) -> str:
        """Read-only efficiency retro: recent per-session records, newest first,
        with the latest compared against the median of the rest.

        DIAGNOSTIC ONLY — these are surfacing numbers to reflect on, NOT a score
        to beat. Token spend varies enormously by task (research shows up to ~30x
        on the SAME task), so treat cross-session deltas as context, not a target.
        Reflect on what drove waste, then store a lesson via add_knowledge /
        add_negative — the engine never auto-optimizes against these.
        """
        tracker.record_call("session_metrics_report")
        rows = storage.list_session_metrics(after_id=after_id, limit=limit)
        if not rows:
            return _with_nudge("No session metrics recorded yet.", tracker)

        lines = [f"Session efficiency (last {len(rows)}, newest first) — "
                 "DIAGNOSTIC, not a target:"]
        for r in rows:
            cc = (r.cc_session_id or "")[:8]
            ratio = ""
            denom = r.code_lines_added + r.comment_lines_added
            if denom:
                pct = round(100 * r.comment_lines_added / denom)
                ratio = f", comments {pct}%"
            per_kloc = ""
            if r.loc_added:
                per_kloc = f", {round(r.out_tokens / r.loc_added)} out-tok/loc"
            lines.append(
                f"  {cc}: {r.out_tokens} out-tok, +{r.loc_added}/-{r.loc_removed} "
                f"loc (churn {r.loc_churned}){ratio}{per_kloc}, "
                f"fixations {r.fixation_events}, max-edit-cycles {r.edit_cycles_max}"
            )

        # Latest-vs-median context (only meaningful with a few prior sessions).
        if len(rows) >= 3:
            latest, rest = rows[0], rows[1:]
            def _med(attr):
                return median([getattr(x, attr) for x in rest]) or 0
            deltas = []
            for label, attr in (("out-tok", "out_tokens"),
                                 ("churn", "loc_churned"),
                                 ("fixations", "fixation_events")):
                cur = getattr(latest, attr)
                med = _med(attr)
                if med:
                    sign = "+" if cur >= med else ""
                    deltas.append(f"{label} {sign}{round(100*(cur-med)/med)}% vs median")
                else:
                    deltas.append(f"{label} {cur} (median 0)")
            lines.append("  latest vs median of prior: " + ", ".join(deltas))
        lines.append("  Reflect: what drove waste? Store a lesson via "
                     "add_knowledge / add_negative.")
        return _with_nudge("\n".join(lines), tracker)
