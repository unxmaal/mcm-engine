#!/usr/bin/env python3
"""Bulk-load an on-disk rules/ tree into a *remote* mcm-engine deployment
over the MCP streamable-http transport, using the `import_rules` tool.

Why this exists: a containerized/remote mcm-engine pod cannot see the local
rules/ tree, and we are a network client of it — we do not touch its
filesystem or its DB directly (see the rule "Don't bypass the MCP via direct
DB writes"). `import_rules` is the first-class, content-in-payload path for
exactly this: one tracked call, one transaction, idempotent on title.

Parses each `<dir>/<slug>.md`:
  - title    = first `# ` heading
  - keywords = `**Keywords:**` line, else synthesized from title + category
  - category = `**Category:**` line, else the top-level directory name
  - content  = the full file text (authoritative body)

Dry run by default (parse + report). Pass --send to actually import.

Examples:
  scripts/import_rules.py                      # dry run against default pod
  scripts/import_rules.py --send               # import
  scripts/import_rules.py --url http://host:8080/mcp --rules-dir ~/projects/rules --send
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
DEFAULT_RULES_DIR = os.environ.get("MCM_RULES_DIR", os.path.expanduser("~/projects/rules"))

_STOP = {"the", "a", "an", "and", "or", "is", "are", "to", "of", "in", "on",
         "for", "with", "not", "never", "must", "rules", "rule", "issues"}


def _synth_keywords(title: str, category: str) -> str:
    words = re.findall(r"[a-z0-9]+", title.lower())
    kw = [w for w in words if w not in _STOP and len(w) > 2]
    return ",".join(dict.fromkeys([category] + kw))


def parse_rules(rules_dir: pathlib.Path) -> list[dict]:
    rules: list[dict] = []
    for f in sorted(rules_dir.rglob("*.md")):
        text = f.read_text(encoding="utf-8", errors="replace")
        rel = f.relative_to(rules_dir)
        title_m = re.search(r"^#\s+(.+?)\s*$", text, re.M)
        title = title_m.group(1).strip() if title_m else f.stem
        cat_m = re.search(r"^\*\*Category:\*\*\s*(.+?)\s*$", text, re.M)
        category = (cat_m.group(1).strip() if cat_m
                    else (rel.parts[0] if len(rel.parts) > 1 else "general"))
        kw_m = re.search(r"^\*\*Keywords:\*\*\s*(.+?)\s*$", text, re.M)
        keywords = kw_m.group(1).strip() if kw_m else _synth_keywords(title, category)
        rules.append({
            "title": title,
            "keywords": keywords,
            "category": category,
            "content": text,
            "file_path": str(rel),
        })
    return rules


def _parse_sse_or_json(body: bytes):
    text = body.decode("utf-8", "replace")
    for line in text.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    return json.loads(text)


def import_over_mcp(url: str, rules: list[dict], *, actor: str, source_repo: str,
                    on_duplicate: str) -> dict:
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
        "clientInfo": {"name": "rule-importer", "version": "1"}}})
    session = resp.headers.get("Mcp-Session-Id")
    post({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}}, session)
    _, body = post({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {
        "name": "import_rules", "arguments": {
            "rules": rules, "on_duplicate": on_duplicate,
            "actor": actor, "source_repo": source_repo}}}, session)
    return _parse_sse_or_json(body)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=DEFAULT_URL, help=f"MCP endpoint (default {DEFAULT_URL})")
    ap.add_argument("--rules-dir", default=DEFAULT_RULES_DIR,
                    help=f"on-disk rules tree (default {DEFAULT_RULES_DIR})")
    ap.add_argument("--actor", default=os.environ.get("MCM_ACTOR", "importer"))
    ap.add_argument("--source-repo", default="local:~/projects/rules")
    ap.add_argument("--on-duplicate", default="update", choices=["update", "skip", "error"])
    ap.add_argument("--send", action="store_true", help="actually POST (default: dry run)")
    args = ap.parse_args()

    rules_dir = pathlib.Path(os.path.expanduser(args.rules_dir))
    if not rules_dir.is_dir():
        print(f"error: rules dir not found: {rules_dir}", file=sys.stderr)
        return 2

    rules = parse_rules(rules_dir)
    if not rules:
        print(f"no *.md rules under {rules_dir}", file=sys.stderr)
        return 1

    cats: dict[str, int] = {}
    for r in rules:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    payload_kib = len(json.dumps(rules)) / 1024
    print(f"parsed {len(rules)} rules from {rules_dir} ({payload_kib:.1f} KiB)")
    print("by category: " + ", ".join(f"{k}={v}" for k, v in sorted(cats.items())))
    bad = [r["file_path"] for r in rules if not r["keywords"].strip() or not r["content"].strip()]
    if bad:
        print(f"WARNING empty keyword/content: {bad}", file=sys.stderr)

    if not args.send:
        print("\n[dry run] re-run with --send to import.")
        return 0

    result = import_over_mcp(args.url, rules, actor=args.actor,
                             source_repo=args.source_repo, on_duplicate=args.on_duplicate)
    # Unwrap the MCP tool-call envelope to the import_rules summary.
    try:
        summary = json.loads(result["result"]["content"][0]["text"])
        print(f"\nimported into {args.url}: total={summary['total']} "
              f"created={summary['created']} updated={summary['updated']} "
              f"skipped={summary['skipped']} errors={summary['errors']}")
    except (KeyError, IndexError, TypeError, ValueError):
        print("\nraw response:\n" + json.dumps(result, indent=2)[:2000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
