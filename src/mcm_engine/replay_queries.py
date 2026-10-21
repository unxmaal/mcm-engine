"""Characterization replay of the legacy vs unified read paths (issue #182,
4.0 program #176).

Extract every search / report_error / get_entry / read_rule call from Claude
Code JSONL transcripts, replay each against one store under
``MCM_ENTRY_MODEL=legacy`` and ``unified`` in the same process, and report
agreement: exact row equality for get_entry / read_rule, Jaccard on the
returned id set plus rank-prefix agreement for search / report_error. The
replay is read-only: counter bumps are disabled while it runs.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from .backends import EntityType
from .entries import from_entry
from .eval.trace_transcript import _content_blocks
from .hooks.mcp_enforcement import _CLAUDE_MCP_TOOL_RE
from .tools import search as search_mod
from .wiring import Context

REPLAYED = ("search", "report_error", "get_entry", "read_rule")


@dataclass
class ReplayQuery:
    tool: str
    args: dict
    source: str
    seq: int


def _tool_suffix(name: str) -> str:
    m = _CLAUDE_MCP_TOOL_RE.match(name or "")
    return m.group(2) if m else (name or "")


def extract_queries(paths: Iterable[str | Path]) -> list[ReplayQuery]:
    """Every replayable tool_use across the transcripts, in file order.
    Unparseable lines are skipped, never raised."""
    out: list[ReplayQuery] = []
    for path in paths:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        seq = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            for block in _content_blocks(rec):
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                suffix = _tool_suffix(block.get("name", "") or "")
                if suffix not in REPLAYED:
                    continue
                args = block.get("input") if isinstance(block.get("input"), dict) else {}
                out.append(ReplayQuery(tool=suffix, args=dict(args),
                                       source=Path(path).name, seq=seq))
                seq += 1
    return out


# ---- one query, one read path -----------------------------------------------------

def _ranked_ids(ctx: Context, q: ReplayQuery, project_name: str, *, gate: bool = True) -> tuple[list, str]:
    """(ranked ids, rendered text) for a search-shaped query, the way the tool
    computes them (search: no quality gate; report_error: gated, full detail)."""
    a = q.args
    ids: list = []
    if q.tool == "search":
        scope = a.get("scope") or "all"
        text = search_mod._search_all_scopes(
            ctx.search, ctx.storage, ctx.counters, str(a.get("query") or ""),
            int(a.get("limit") or 10), [],
            min_rank=0.0, project=str(a.get("project") or ""),
            entity_types=search_mod._SCOPE_MAP.get(scope, search_mod._SCOPE_MAP["all"]),
            include_archived=bool(a.get("include_archived", False)),
            detail=a.get("detail") or "summary", ids_out=ids,
        )
    else:
        text = search_mod._search_all_scopes(
            ctx.search, ctx.storage, ctx.counters, str(a.get("error_text") or ""),
            5, [], min_rank=(search_mod.RANK_THRESHOLD if gate else 0.0),
            project=str(a.get("project") or project_name), detail="full", ids_out=ids,
        )
    return [list(i) for i in ids], text


def _point_read(ctx: Context, q: ReplayQuery) -> Any:
    """The legacy row (projected through Entry so both paths compare equal
    when they agree) for get_entry / read_rule, or None."""
    a = q.args
    if q.tool == "get_entry":
        try:
            etype = EntityType(str(a.get("entity_type")))
            eid = int(a.get("entry_id"))
        except (ValueError, TypeError):
            return None
        if search_mod.use_unified():
            e = ctx.storage.find_entry(etype.value, eid)
            return from_entry(e) if e else None
        return ctx.storage.find_by_id(etype, eid)
    # read_rule resolves by file path on the legacy table (no unified lookup
    # by path exists); the unified side must hold the identical projection.
    row = ctx.storage.find_rule_by_file_path(str(a.get("file_path") or ""))
    if row is None or not search_mod.use_unified():
        return row
    e = ctx.storage.find_entry("rule", row.id)
    return from_entry(e) if e else None


# ---- agreement ------------------------------------------------------------------

def jaccard(a: list, b: list) -> float:
    sa, sb = {tuple(x) for x in a}, {tuple(x) for x in b}
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)


def topk_jaccard(a: list, b: list, k: int = 5) -> float:
    return jaccard(a[:k], b[:k])


def prefix_agreement(a: list, b: list) -> float:
    """Share of the longer list that is an identical ranked prefix."""
    n = max(len(a), len(b))
    if n == 0:
        return 1.0
    k = 0
    for x, y in zip(a, b):
        if list(x) != list(y):
            break
        k += 1
    return k / n


@dataclass
class QueryResult:
    tool: str
    args: dict
    source: str
    seq: int
    agree: bool
    jaccard: float = 1.0
    prefix: float = 1.0
    text_identical: bool = True
    top5: float = 1.0
    gate_flip: bool = False        # report_error only: the raw-score gate emptied one side
    ungated_jaccard: float = 1.0   # report_error only: agreement with the gate off
    legacy_ids: list = field(default_factory=list)
    unified_ids: list = field(default_factory=list)


@dataclass
class ReplayReport:
    store: str
    counts: dict = field(default_factory=dict)
    agreement: dict = field(default_factory=dict)
    disagreements: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


def _with_model(model: str):
    prev = os.environ.get("MCM_ENTRY_MODEL")
    os.environ["MCM_ENTRY_MODEL"] = model
    return prev


def _restore(prev: Optional[str]) -> None:
    if prev is None:
        os.environ.pop("MCM_ENTRY_MODEL", None)
    else:
        os.environ["MCM_ENTRY_MODEL"] = prev


def _strip_match(text: str) -> str:
    """Drop the backend snippet lines: the unified index windows a facet-only
    match over its neighbours (#181), which is a known, bounded difference."""
    return "\n".join(l for l in text.split("\n") if not l.startswith("  Match: "))


def replay(ctx: Context, queries: list[ReplayQuery], *, store: str = "",
           project_name: str = "", jaccard_threshold: float = 0.9) -> ReplayReport:
    """Run every query under both read paths and score the agreement."""
    # read-only: never let the replay move hit counts
    ctx.counters.increment = lambda *a, **k: None  # type: ignore[method-assign]
    results: list[QueryResult] = []
    prev = os.environ.get("MCM_ENTRY_MODEL")
    try:
        for q in queries:
            if q.tool in ("search", "report_error"):
                _with_model("legacy")
                l_ids, l_text = _ranked_ids(ctx, q, project_name)
                _with_model("unified")
                u_ids, u_text = _ranked_ids(ctx, q, project_name)
                j = jaccard(l_ids, u_ids)
                p = prefix_agreement(l_ids, u_ids)
                same_text = _strip_match(l_text) == _strip_match(u_text)
                flip, uj = False, j
                if q.tool == "report_error" and (bool(l_ids) != bool(u_ids)):
                    # the quality gate is an absolute raw FTS score (RANK_THRESHOLD)
                    # and the two indexes score on different scales; measure the
                    # flip and what the paths return with the gate off
                    flip = True
                    _with_model("legacy")
                    lu, _ = _ranked_ids(ctx, q, project_name, gate=False)
                    _with_model("unified")
                    uu, _ = _ranked_ids(ctx, q, project_name, gate=False)
                    uj = jaccard(lu, uu)
                results.append(QueryResult(
                    q.tool, q.args, q.source, q.seq, agree=(j >= jaccard_threshold),
                    jaccard=j, prefix=p, text_identical=same_text,
                    top5=topk_jaccard(l_ids, u_ids), gate_flip=flip, ungated_jaccard=uj,
                    legacy_ids=l_ids, unified_ids=u_ids))
            else:
                _with_model("legacy")
                l_row = _point_read(ctx, q)
                _with_model("unified")
                u_row = _point_read(ctx, q)
                results.append(QueryResult(q.tool, q.args, q.source, q.seq,
                                           agree=(l_row == u_row)))
    finally:
        _restore(prev)

    report = ReplayReport(store=store)
    for tool in REPLAYED:
        rs = [r for r in results if r.tool == tool]
        report.counts[tool] = len(rs)
        if not rs:
            continue
        agg: dict[str, Any] = {"agree": sum(r.agree for r in rs) / len(rs)}
        if tool in ("search", "report_error"):
            agg["mean_jaccard"] = sum(r.jaccard for r in rs) / len(rs)
            agg["exact_order"] = sum(r.prefix == 1.0 for r in rs) / len(rs)
            agg["text_identical"] = sum(r.text_identical for r in rs) / len(rs)
            agg["top5_jaccard"] = sum(r.top5 for r in rs) / len(rs)
            agg["empty_both"] = sum(not r.legacy_ids and not r.unified_ids for r in rs) / len(rs)
            if tool == "report_error":
                agg["gate_flips"] = sum(r.gate_flip for r in rs)
                agg["mean_ungated_jaccard"] = sum(r.ungated_jaccard for r in rs) / len(rs)
        report.agreement[tool] = agg
    report.disagreements = [asdict(r) for r in results if not r.agree]
    return report


# ---- store resolution ---------------------------------------------------------------

def open_context(store: str, *, query_mode: str = "strict") -> Context:
    """A full Context (storage + counters + search) for a SQLite path or a
    Postgres DSN, schema ensured (so a pre-4.0 snapshot migrates, backfills
    and reconciles exactly as a deployment would)."""
    from .adapters.sqlite.session import InMemorySession

    if store.startswith(("postgres://", "postgresql://")):
        from .adapters.postgres._pool import make_pool
        from .adapters.postgres.counters import PostgresCounters
        from .adapters.postgres.search import PostgresSearch
        from .adapters.postgres.storage import PostgresStorage

        pool = make_pool(store)
        storage = PostgresStorage(dsn=store, pool=pool)
        storage.ensure_schema()
        return Context(storage=storage, counters=PostgresCounters(store, pool=pool),
                       search=PostgresSearch(store, pool=pool, query_mode=query_mode),
                       session=InMemorySession())

    from .adapters.sqlite.counters import SqliteCounters
    from .adapters.sqlite.search import SqliteSearch
    from .adapters.sqlite.storage import SqliteStorage
    from .db import KnowledgeDB

    path = store[len("sqlite:///"):] if store.startswith("sqlite:///") else store
    db = KnowledgeDB(path)
    storage = SqliteStorage(db=db)
    storage.ensure_schema()
    return Context(storage=storage, counters=SqliteCounters(db=db),
                   search=SqliteSearch(db=db), session=InMemorySession())


def format_report(report: ReplayReport) -> str:
    lines = [f"store: {report.store}"]
    for tool in REPLAYED:
        n = report.counts.get(tool, 0)
        agg = report.agreement.get(tool) or {}
        if not n:
            lines.append(f"  {tool:13s} n=0")
            continue
        extra = "  ".join(f"{k}={v:.3f}" if isinstance(v, float) else f"{k}={v}"
                          for k, v in agg.items() if k != "agree")
        lines.append(f"  {tool:13s} n={n:<5d} agree={agg['agree']:.3f}  {extra}")
    lines.append(f"  disagreements: {len(report.disagreements)}")
    return "\n".join(lines)
