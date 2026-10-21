"""Corpus snapshot + census (issue #177, first artifact of the 4.0 program #176).

`snapshot` walks every entity type through the MCP `scroll_entries` tool over
HTTP (never a direct DB read, DECISION #1) and writes the rows into a local
SQLite in the core schema, so `migrate.py` can copy them into a rehearsal
Postgres. `census` reads such a store and reports what the corpus actually
contains, per table and column, so the unified model is designed from data.

The snapshot carries the TEXT fields `scroll_entries` renders (corpus._TEXT_FIELDS)
plus status/source_classification from the meta line. It does not carry
project, kind, severity, importance, counters or timestamps, which the render
omits; the full-fidelity round-trip check lives inside the real migration (#179).
"""
from __future__ import annotations

import json
import re
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

from .backends import EntityType, ErrorRow, KnowledgeRow, NegativeRow, RuleRow
from .sanitize import is_polluted
from .tools.corpus import _TEXT_FIELDS

_HEAD_RE = re.compile(r"^#(\d+) \[(knowledge|negative|error|rule)\] (.*)$")
_META_RE = re.compile(r"^  hash=([0-9a-f]+)(.*)$")
_FOOTER_RE = re.compile(r"^--- page: ")


@dataclass
class ParsedEntry:
    etype: EntityType
    id: int
    fields: dict[str, str] = field(default_factory=dict)
    status: Optional[str] = None
    classification: Optional[str] = None


def parse_scroll_page(text: str) -> list[ParsedEntry]:
    """Parse the text `scroll_entries` renders (corpus._render_entry) back into
    entries. Line-driven, not blank-line split: bodies contain blank lines. A
    new field starts only on `  <known field>: `, everything else continues the
    current field. A body line that itself looks like a header or a known field
    label would mis-split; that is the accepted cost of a text transport."""
    out: list[ParsedEntry] = []
    cur: Optional[ParsedEntry] = None
    cur_field: Optional[str] = None
    head_lines: list[str] = []
    in_head = False

    def flush_head() -> None:
        nonlocal in_head
        if cur is not None and in_head:
            cur.fields[_TEXT_FIELDS[cur.etype][0]] = "\n".join(head_lines).rstrip()
        in_head = False

    for line in text.split("\n"):
        if _FOOTER_RE.match(line):
            break
        m = _HEAD_RE.match(line)
        if m and (cur is None or not in_head):
            flush_head()
            cur = ParsedEntry(etype=EntityType(m.group(2)), id=int(m.group(1)))
            out.append(cur)
            head_lines = [m.group(3)]
            in_head = True
            cur_field = None
            continue
        if cur is None:
            continue
        mm = _META_RE.match(line)
        if mm and in_head:
            flush_head()
            for part in mm.group(2).split(" | "):
                part = part.strip()
                if part.startswith("status="):
                    cur.status = part[len("status="):]
                elif part.startswith("class="):
                    cur.classification = part[len("class="):]
            continue
        if in_head:
            head_lines.append(line)
            continue
        matched = False
        for f in _TEXT_FIELDS[cur.etype][1:]:
            prefix = f"  {f}: "
            if line.startswith(prefix):
                cur_field = f
                cur.fields[f] = line[len(prefix):]
                matched = True
                break
        if matched:
            continue
        if cur_field is not None:
            cur.fields[cur_field] += "\n" + line
    flush_head()
    for e in out:
        for f in list(e.fields):
            e.fields[f] = e.fields[f].rstrip()
    return out


def _row_from_parsed(e: ParsedEntry):
    f = e.fields
    if e.etype is EntityType.KNOWLEDGE:
        return KnowledgeRow(
            id=e.id, topic=f.get("topic", ""), summary=f.get("summary", ""),
            detail=f.get("detail") or None, tags=f.get("tags") or None,
            rationale=f.get("rationale") or None,
            alternatives=f.get("alternatives") or None,
            source_classification=e.classification,
        )
    if e.etype is EntityType.NEGATIVE:
        return NegativeRow(
            id=e.id, category=f.get("category", ""),
            what_failed=f.get("what_failed", ""),
            why_failed=f.get("why_failed") or None,
            correct_approach=f.get("correct_approach") or None,
            source_classification=e.classification,
        )
    if e.etype is EntityType.ERROR:
        return ErrorRow(
            id=e.id, pattern=f.get("pattern", ""), context=f.get("context") or None,
            root_cause=f.get("root_cause") or None, fix=f.get("fix") or None,
            tags=f.get("tags") or None, source_classification=e.classification,
        )
    return RuleRow(
        id=e.id, title=f.get("title", ""), keywords=f.get("keywords", "") or "",
        description=f.get("description") or None, content=f.get("content") or None,
        category=f.get("category") or None, status=e.status or "active",
        source_classification=e.classification,
    )


@dataclass
class SnapshotReport:
    counts: dict[str, int] = field(default_factory=dict)
    pages: int = 0

    def as_dict(self) -> dict:
        return {"counts": self.counts, "pages": self.pages,
                "not_carried": ["project", "kind", "severity", "importance", "scope",
                                "pinned", "hit_count", "created_at", "refs_json",
                                "file_path", "archived"]}


def snapshot(
    call_tool: Callable[[str, dict], str],
    out_path: str | Path,
    *,
    page: int = 100,
) -> SnapshotReport:
    """Walk scroll_entries for every entity type via `call_tool(name, args)`
    and write the rows (ids preserved) into a fresh SQLite at `out_path`."""
    from .adapters.sqlite.storage import SqliteStorage

    out = Path(out_path)
    if out.exists():
        raise FileExistsError(f"{out} exists; refusing to overwrite a snapshot")
    storage = SqliteStorage(db_path=str(out))
    storage.ensure_schema()
    report = SnapshotReport()
    inserters = {
        EntityType.KNOWLEDGE: storage.insert_knowledge,
        EntityType.NEGATIVE: storage.insert_negative,
        EntityType.ERROR: storage.insert_error,
        EntityType.RULE: storage.insert_rule,
    }
    for et in (EntityType.KNOWLEDGE, EntityType.NEGATIVE, EntityType.ERROR, EntityType.RULE):
        after = 0
        n = 0
        while True:
            text = call_tool("scroll_entries", {
                "entity_type": et.value, "after_id": after, "limit": page,
            })
            report.pages += 1
            entries = parse_scroll_page(text)
            if not entries:
                break
            for e in entries:
                inserters[et](_row_from_parsed(e))
                n += 1
            after = entries[-1].id
            if len(entries) < page:
                break
        report.counts[et.value] = n
    try:
        storage.bump_sequences()
    except Exception:
        pass
    return report


# --- census -----------------------------------------------------------------

def _pct(values: list[int], p: float) -> int:
    if not values:
        return 0
    s = sorted(values)
    k = max(0, min(len(s) - 1, int(round(p * (len(s) - 1)))))
    return s[k]


def _polluted(et: EntityType, row) -> bool:
    if et is EntityType.KNOWLEDGE:
        return is_polluted(row.summary) or is_polluted(getattr(row, "detail", None))
    if et is EntityType.NEGATIVE:
        return is_polluted(row.what_failed)
    if et is EntityType.ERROR:
        return is_polluted(row.pattern)
    return is_polluted(getattr(row, "content", None))


def _fits(et: EntityType, row) -> bool:
    """Lossless-fit against the 4.0 projection: a non-empty title and at least
    one of summary/body non-empty, and no serialized blob trapped in the
    headline (a trapped blob means sibling data lives in the wrong column)."""
    fields = _TEXT_FIELDS[et]
    title = getattr(row, fields[0], None)
    rest = [getattr(row, f, None) for f in fields[1:]]
    return bool(title and str(title).strip()) and any(rest) and not _polluted(et, row)


def census(storage, etypes: Iterable[EntityType] = tuple(EntityType)) -> dict:
    """Per table: row count, per-column null rate and length percentiles,
    pollution rate, and the ids that fail the lossless-fit check."""
    report: dict = {"tables": {}}
    for et in etypes:
        fields = _TEXT_FIELDS[et]
        rows = list(storage.iter_entries(et))
        cols: dict[str, dict] = {}
        for f in fields:
            vals = [getattr(r, f, None) for r in rows]
            lens = [len(str(v)) for v in vals if v]
            cols[f] = {
                "null_rate": round(sum(1 for v in vals if not v) / len(vals), 3) if vals else 0.0,
                "len_p50": _pct(lens, 0.5),
                "len_p90": _pct(lens, 0.9),
                "len_max": max(lens) if lens else 0,
                "len_mean": round(statistics.fmean(lens), 1) if lens else 0.0,
            }
        polluted = [r.id for r in rows if _polluted(et, r)]
        unfit = [r.id for r in rows if not _fits(et, r)]
        report["tables"][et.value] = {
            "rows": len(rows),
            "columns": cols,
            "polluted": {"count": len(polluted), "ids": polluted[:100]},
            "unfit": {"count": len(unfit), "ids": unfit[:100]},
        }
    report["total_rows"] = sum(t["rows"] for t in report["tables"].values())
    report["total_unfit"] = sum(t["unfit"]["count"] for t in report["tables"].values())
    return report


def format_census(report: dict) -> str:
    return json.dumps(report, indent=2, sort_keys=True)
