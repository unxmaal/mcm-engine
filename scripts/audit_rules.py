#!/usr/bin/env python3
"""Audit the live rule corpus against the c5 context-engineering axes.

Reads every rule from a *remote* mcm-engine pod over the MCP streamable-http
transport via the `list_rules` tool (the corpus lives in the pod's Postgres,
not on local disk — same reason import_rules.py is a network client and never
touches the DB directly). Classifies each rule against the hierarchy `kind`
column (issue #64), which already IS the FACT (recall-only) vs CONSTRAINT
(enforceable directive) distinction the plan asks for, and flags:

  - over-prescriptive CONSTRAINT candidates: kind=directive with no reinforcement
    and no hits (never actually used -> deletion/demotion candidate).
  - load-bearing FACTs: kind=fact with heavy reinforcement (keep).
  - likely mis-classification: the title reads imperative but kind=fact, or
    reads like a gotcha but kind=directive (REVIEW, never auto-changed).
  - unused: hits=0 and reinf=0 (proxy for stale; true last-hit recency is not in
    list_rules' output — see LIMITATION below).

Report-only: writes audit/rules_audit.md and changes nothing. Corpus edits route
through set_rule_metadata / the git-reviewed import flow after human review.

LIMITATION: list_rules returns id/importance/scope/kind/category/hits/reinf/
correct-incorrect/status/title, but NOT the rule body or last_hit_at. So
content-based heuristics ("imperative prohibition with no environmental fact
attached") and true staleness can't be computed here; classification uses the
kind column plus a title heuristic. Deepening this needs a list_rules that also
returns content/last_hit_at — filed as a follow-up, not faked here.

Examples:
  scripts/audit_rules.py                       # audit default pod -> audit/rules_audit.md
  scripts/audit_rules.py --url http://host:8080/mcp --out audit/rules_audit.md
  scripts/audit_rules.py --include-archived
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import urllib.request

DEFAULT_URL = os.environ.get("MCM_MCP_URL", "http://192.168.8.88:8080/mcp")

# Title-heuristic markers. Imperative/behavioral phrasing suggests a directive;
# gotcha/observation phrasing suggests a recall-only fact.
_IMPERATIVE = re.compile(
    r"\b(use|never|always|must|do not|don't|avoid|prefer|ensure|require)\b", re.I)
_FACTUAL = re.compile(
    r"\b(trap|bug|gotcha|returns?|fails?|causes?|is |are |quirk|behavior)\b", re.I)


# ---- pure logic (unit-tested) -----------------------------------------------

def parse_list_rules(text: str) -> list[dict]:
    """Parse the `list_rules` pipe-delimited table into row dicts. Tolerant of
    a leading header line and pipes inside the trailing title field."""
    rules: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("#") or line.startswith("# rules"):
            continue  # header or non-row
        # 10 fields; title is last and may itself contain " | "
        parts = [p.strip() for p in line.split(" | ", 9)]
        if len(parts) < 10:
            continue
        rid, imp, scope, kind, category, hits, reinf, corr, status, title = parts
        c, _, i = corr.partition("/")
        rules.append({
            "id": int(rid.lstrip("#")),
            "importance": _int(imp.replace("imp=", "")),
            "scope": scope,
            "kind": kind,
            "category": None if category == "-" else category,
            "hits": _int(hits.replace("h=", "")),
            "reinf": _int(reinf.replace("rf=", "")),
            "correct": _int(c),
            "incorrect": _int(i),
            "status": status,
            "title": title,
        })
    return rules


def _int(s: str) -> int:
    try:
        return int(s)
    except (ValueError, TypeError):
        return 0


def classify(r: dict) -> tuple[str, list[str]]:
    """Return (recommendation, reasons). Recommendations:
    KEEP, DELETE, DEMOTE, RECLASSIFY, REVIEW."""
    reasons: list[str] = []
    kind = r["kind"]
    used = r["hits"] > 0 or r["reinf"] > 0
    title_imperative = bool(_IMPERATIVE.search(r["title"]))
    title_factual = bool(_FACTUAL.search(r["title"]))

    # Mis-classification signals (title vs kind disagree).
    if kind == "fact" and title_imperative and not title_factual:
        reasons.append("title reads imperative but kind=fact")
        return "RECLASSIFY", reasons
    if kind == "directive" and title_factual and not title_imperative:
        reasons.append("title reads like a gotcha but kind=directive")
        return "RECLASSIFY", reasons

    # Over-prescriptive constraint that nothing has ever leaned on.
    if kind == "directive" and not used:
        reasons.append("directive with zero hits and zero reinforcement")
        if r["importance"] >= 1:
            reasons.append(f"importance={r['importance']} despite never being used")
            return "DEMOTE", reasons
        return "DELETE", reasons

    # Load-bearing fact — clearly keep.
    if kind == "fact" and r["reinf"] >= 3:
        reasons.append(f"fact with reinforcement={r['reinf']} (load-bearing)")
        return "KEEP", reasons

    if not used:
        reasons.append("unused (hits=0, reinf=0) — verify still relevant")
        return "REVIEW", reasons

    reasons.append("in use; kind matches title")
    return "KEEP", reasons


def render_report(rules: list[dict]) -> str:
    rows = [(r, *classify(r)) for r in rules]
    order = {"DELETE": 0, "DEMOTE": 1, "RECLASSIFY": 2, "REVIEW": 3, "KEEP": 4}
    rows.sort(key=lambda x: (order.get(x[1], 9), -x[0]["reinf"]))

    counts: dict[str, int] = {}
    kinds: dict[str, int] = {}
    for r, rec, _ in rows:
        counts[rec] = counts.get(rec, 0) + 1
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1

    out = ["# Rules audit (c5 context-engineering)", ""]
    out.append(f"Total rules: {len(rules)}")
    out.append("Recommendations: " + ", ".join(
        f"{k}={v}" for k, v in sorted(counts.items())))
    out.append("Kinds: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    out.append("")
    out.append("| id | kind | imp | scope | hits | reinf | c/i | status | rec | title | why |")
    out.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for r, rec, reasons in rows:
        out.append(
            f"| #{r['id']} | {r['kind']} | {r['importance']} | {r['scope']} | "
            f"{r['hits']} | {r['reinf']} | {r['correct']}/{r['incorrect']} | "
            f"{r['status']} | **{rec}** | {r['title']} | {'; '.join(reasons)} |")
    out.append("")
    out.append("_Report only. `RECLASSIFY`/`DEMOTE` via `set_rule_metadata`; "
               "deletions via the git-reviewed corpus. Title heuristics are "
               "advisory — see the script's LIMITATION note._")
    return "\n".join(out)


# ---- MCP client -------------------------------------------------------------

def _parse_sse_or_json(body: bytes):
    text = body.decode("utf-8", "replace")
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return json.loads(text)


def fetch_list_rules(url: str, *, include_archived: bool) -> str:
    hdr = {"Content-Type": "application/json",
           "Accept": "application/json, text/event-stream"}

    def post(payload, session=None):
        h = dict(hdr)
        if session:
            h["Mcp-Session-Id"] = session
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers=h, method="POST")
        resp = urllib.request.urlopen(req, timeout=120)
        return resp, resp.read()

    resp, _ = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2024-11-05", "capabilities": {},
        "clientInfo": {"name": "rule-auditor", "version": "1"}}})
    session = resp.headers.get("Mcp-Session-Id")
    post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, session)
    _, body = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "list_rules", "arguments": {
            "include_archived": include_archived, "min_importance": 0, "limit": 0}}},
        session)
    envelope = _parse_sse_or_json(body)
    return envelope["result"]["content"][0]["text"]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL, help=f"MCP endpoint (default {DEFAULT_URL})")
    ap.add_argument("--out", default="audit/rules_audit.md", help="report path")
    ap.add_argument("--include-archived", action="store_true")
    args = ap.parse_args()

    try:
        text = fetch_list_rules(args.url, include_archived=args.include_archived)
    except Exception as e:  # noqa: BLE001 - surface any transport/parse failure
        print(f"error fetching list_rules from {args.url}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        return 2

    rules = parse_list_rules(text)
    if not rules:
        print("no rules parsed (empty corpus or unexpected list_rules format)",
              file=sys.stderr)
        return 1

    report = render_report(rules)
    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report + "\n", encoding="utf-8")

    counts: dict[str, int] = {}
    for r in rules:
        rec, _ = classify(r)
        counts[rec] = counts.get(rec, 0) + 1
    print(f"audited {len(rules)} rules -> {out}")
    print("  " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
