"""CLI entry point: mcm-engine run / mcm-engine init."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from . import __version__
from .config import MCMConfig, load_config
from .migrate import format_report, migrate, open_storage
from .server import MCMServer


def cmd_run(args):
    """Run the MCP server."""
    config_path = Path(args.config) if args.config else None
    project_root = Path(args.project_root) if args.project_root else None

    config = load_config(config_path=config_path, project_root=project_root)
    server = MCMServer(config, project_root=project_root or Path.cwd())
    server.run()


def cmd_init(args):
    """Create mcm-engine.yaml and .claude/ directory."""
    project_root = Path(args.project_root) if args.project_root else Path.cwd()
    config_file = project_root / "mcm-engine.yaml"
    claude_dir = project_root / ".claude"

    if config_file.exists() and not args.force:
        print(f"Config already exists: {config_file}", file=sys.stderr)
        print("Use --force to overwrite.", file=sys.stderr)
        sys.exit(1)

    # Create .claude directory
    claude_dir.mkdir(parents=True, exist_ok=True)

    # Create rules directory
    rules_dir = project_root / "rules"
    rules_dir.mkdir(parents=True, exist_ok=True)

    # Write config
    config_data = {
        "project_name": args.project,
        "db_path": ".claude/knowledge.db",
        "rules_path": "rules/",
        "plugins": [],
        "nudges": {
            "store_reminder_turns": 10,
            "checkpoint_turns": 25,
            "mandatory_stop_turns": 50,
        },
    }
    with open(config_file, "w") as f:
        yaml.dump(config_data, f, default_flow_style=False, sort_keys=False)

    print(f"Created {config_file}")
    print(f"Created {claude_dir}/")
    print(f"Created {rules_dir}/")
    print()
    print("Add to your .mcp.json:")
    print('  {')
    print('    "mcpServers": {')
    print('      "knowledge": {')
    print('        "command": "mcm-engine",')
    print('        "args": ["run"]')
    print('      }')
    print('    }')
    print('  }')


def cmd_serve(args):
    """Run the engine as a long-lived HTTP/SSE daemon."""
    import os

    from .transport import serve

    config_path = Path(args.config) if args.config else None
    project_root = Path(args.project_root) if args.project_root else None

    config = load_config(config_path=config_path, project_root=project_root)
    server = MCMServer(config, project_root=project_root or Path.cwd())

    # Env fallbacks for container deploys, where the reachable host (a docker
    # -p published address) is not visible from inside the container and can't
    # be auto-detected. MCM_ALLOWED_HOSTS is comma/space separated.
    allowed_hosts = list(args.allowed_host)
    env_hosts = os.environ.get("MCM_ALLOWED_HOSTS", "")
    allowed_hosts.extend(h for h in env_hosts.replace(",", " ").split() if h)

    protection = not args.no_dns_rebinding_protection
    if os.environ.get("MCM_DNS_REBINDING_PROTECTION", "").strip().lower() in (
        "0", "false", "no", "off",
    ):
        protection = False

    serve(
        server,
        host=args.host,
        port=args.port,
        transport=args.transport,
        allowed_hosts=allowed_hosts,
        dns_rebinding_protection=protection,
    )


def cmd_migrate(args):
    """Copy every row from --from DSN into --to DSN, ids preserved."""
    source = open_storage(args.source)
    dest = open_storage(args.dest)
    try:
        report = migrate(source, dest, force=args.force)
    except ValueError as e:
        print(f"migration aborted: {e}", file=sys.stderr)
        sys.exit(2)
    print("Migration complete:")
    print(format_report(report))


def cmd_export_mirror(args):
    """One-way DB -> git review mirror of active rules (issue #22)."""
    from pathlib import Path

    from .mirror import export_mirror

    storage = open_storage(args.source)
    result = export_mirror(storage, Path(args.out))
    if result["committed"]:
        print(f"Mirrored {result['written']} active rules to {args.out} (new commit).")
    else:
        print(f"Mirror up to date: {result['written']} active rules, no changes.")


def cmd_consolidate(args):
    """Read-only KB-hygiene consolidation report (issue #31)."""
    from .consolidate import consolidation_report, format_report

    storage = open_storage(args.source)
    rep = consolidation_report(storage, max_age_days=args.max_age_days)
    print(format_report(rep))


def cmd_hook(args):
    """Run the PreToolUse enforcement hook. Reads a single event from
    stdin and exits 0 (allow) or 2 (block). Wire into your agent
    harness's settings.json — see README "Making agents actually use it"
    section."""
    from .hooks.mcp_enforcement import main as hook_main
    sys.exit(hook_main())


def cmd_mint_token(args):
    """Mint a bearer token for the HTTP/streamable-HTTP transport.

    Writes one row to the tokens table and prints the plaintext token
    to stdout exactly once. The plaintext is not recoverable from the
    database; the operator captures it here, hands it to the
    consumer, then discards their local copy.
    """
    from . import tokens as token_mod

    config_path = Path(args.config) if args.config else None
    project_root = Path(args.project_root) if args.project_root else None
    config = load_config(config_path=config_path, project_root=project_root)

    if config.backends.storage != "postgres":
        print(
            f"mint-token requires the postgres storage backend "
            f"(found {config.backends.storage!r}). Set MCM_BACKENDS_STORAGE=postgres.",
            file=sys.stderr,
        )
        sys.exit(2)

    server = MCMServer(config, project_root=project_root or Path.cwd())
    try:
        minted = token_mod.mint_token(
            server.ctx.storage._conn, principal=args.principal
        )
    except ValueError as e:
        print(f"mint-token: {e}", file=sys.stderr)
        sys.exit(2)

    print(minted.plaintext)
    print(
        f"# minted for principal={minted.principal!r}. "
        f"Show this token once; revoke via UPDATE tokens SET revoked_at = now() "
        f"WHERE token_hash matches.",
        file=sys.stderr,
    )


def cmd_session_start(args):
    """Run the SessionStart hook. Reads a SessionStart event from stdin and
    prints resume context as additionalContext JSON. Always exits 0 (a hook
    error must never block a session from starting)."""
    from .hooks.session_start import main as ss_main
    sys.exit(ss_main())


def cmd_ingest(args):
    """Surface candidates from an external source for agent evaluation.

    Default mode (curated): emits candidate blocks to stdout. NO DB writes.
    The agent reads each candidate and decides — via the same methods used
    at session-end — whether it's a new finding (`add_knowledge`), a rule
    (`add_rule`), an anti-pattern (`add_negative`), or nothing worth storing.

    --bulk mode: skips per-item evaluation and upserts every candidate
    directly through the configured storage backend. Use only when the
    source IS your declared authoritative corpus (e.g. an Obsidian vault
    you've already accepted as canonical). The default refuses to bulk-load
    because the resulting noise drowns the high-signal KB.

    Idempotent in either mode (--bulk dedups on topic+kind; default mode
    leaves the decision to the agent's add_* calls, which dedup themselves).
    """
    from .ingest import (
        NoMatchingIngester,
        UnknownIngester,
        find as find_ingester,
        registered as list_registered,
    )
    from .wiring import build_context

    if args.list_types:
        names = [cls.name for cls in list_registered()]
        if not names:
            print("(no ingesters registered)")
        for n in names:
            print(n)
        return

    if not args.source:
        print("error: source path required (or pass --list-types)", file=sys.stderr)
        sys.exit(2)

    project_root = Path(args.project_root) if args.project_root else Path.cwd()
    config_path = Path(args.config) if args.config else None
    config = load_config(config_path=config_path, project_root=project_root)

    # Same wiring fix MCMServer does in server.py:81-99: resolve db_path
    # against project_root, and pre-populate the embedded adapters'
    # storage_options.db_path so build_context sees an absolute path.
    resolved_db = config.resolve_db_path(project_root)
    backends = config.backends
    for axis_name, axis_opts in (
        ("storage", backends.storage_options),
        ("counters", backends.counters_options),
        ("search", backends.search_options),
    ):
        if getattr(backends, axis_name) == "embedded" and "db_path" not in axis_opts:
            axis_opts["db_path"] = str(resolved_db)

    try:
        ingester = find_ingester(args.source, explicit_name=args.type)
    except (UnknownIngester, NoMatchingIngester) as e:
        print(f"ingest: {e}", file=sys.stderr)
        sys.exit(2)

    skip = set(args.skip.split(",")) if args.skip else None
    opts = {
        "kind": args.kind,
        "project": args.project,
        "skip": skip,
    }

    # Walk first, then page. Streamers are cheap because they're generators,
    # but `--bulk` needs counts and `--offset`/`--batch` needs slicing.
    candidates = list(ingester.stream(args.source, opts))
    total = len(candidates)
    start = max(0, args.offset)
    end = total if args.bulk else min(start + args.batch, total)
    window = candidates[start:end]

    print(f"# ingester: {ingester.name}", file=sys.stderr)
    print(f"# source:   {args.source}", file=sys.stderr)
    print(f"# total:    {total} candidates", file=sys.stderr)
    if args.bulk:
        print(f"# mode:     --bulk (writes to {resolved_db})", file=sys.stderr)
        if args.dry_run:
            print(f"# dry-run:  yes (no writes)", file=sys.stderr)
    else:
        print(f"# mode:     curated (no writes — agent decides per candidate)", file=sys.stderr)
        print(f"# showing:  {start + 1}-{end} of {total}", file=sys.stderr)
        if end < total:
            print(f"# next:     --offset {end} --batch {args.batch}", file=sys.stderr)
    print(file=sys.stderr)

    if args.bulk:
        _ingest_bulk(window, config, dry_run=args.dry_run, total=total)
    else:
        _ingest_emit(window, start_index=start, total=total)

    # Post-stream report: ingesters can surface observations (extension
    # counts, AST-upgrade suggestions, etc.) to stderr. Optional method;
    # absent or empty → no report.
    report_fn = getattr(ingester, "report", None)
    if callable(report_fn):
        text = report_fn()
        if isinstance(text, str) and text.strip():
            print(file=sys.stderr)
            print(text, file=sys.stderr)


def _ingest_emit(window, *, start_index: int, total: int) -> None:
    """Curated mode: emit each candidate as a delimited block on stdout.
    The agent reads, evaluates, and calls add_knowledge / add_rule /
    add_negative selectively. No writes happen from here."""
    for offset, row in enumerate(window):
        idx = start_index + offset + 1
        print(f"=== candidate {idx}/{total} ===")
        print(f"topic: {row.topic}")
        if row.tags:
            print(f"tags: {row.tags}")
        if row.project:
            print(f"suggested_project: {row.project}")
        if row.kind:
            print(f"suggested_kind: {row.kind}")
        print(f"summary: {row.summary}")
        print()
        if row.detail:
            print(row.detail.rstrip())
        print()


def _ingest_bulk(window, config, *, dry_run: bool, total: int) -> None:
    """--bulk mode: write every candidate. Same path the v1 ingest used.
    For declared-authoritative corpora only."""
    from .db import KnowledgeDB
    from .schema import migrate_core
    from .wiring import build_context

    # Ensure the SQLite schema exists. build_context wires storage adapters
    # against the configured db_path but doesn't migrate the schema — the
    # engine usually does that during MCMServer init.
    if config.backends.storage == "embedded":
        db_path = config.backends.storage_options.get("db_path")
        if db_path:
            migrate_core(KnowledgeDB(db_path))

    ctx = build_context(config)
    inserted = updated = errors = 0
    for i, row in enumerate(window, 1):
        existing = ctx.storage.find_knowledge_by_topic_kind(row.topic, row.kind)
        if dry_run:
            verb = "update" if existing else "insert"
            print(f"  would {verb}: {row.topic}")
            if existing:
                updated += 1
            else:
                inserted += 1
        else:
            if existing is not None:
                ctx.storage.update_knowledge(
                    existing.id,
                    summary=row.summary,
                    detail=row.detail,
                    tags=row.tags,
                    project=row.project,
                )
                updated += 1
            else:
                ctx.storage.insert_knowledge(row)
                inserted += 1
        if i % 100 == 0:
            print(f"  {i}/{total}  (inserted={inserted} updated={updated} errors={errors})")
    print()
    print(
        f"bulk done. inserted={inserted}, updated={updated}, errors={errors}"
        f"{' [DRY RUN]' if dry_run else ''}"
    )


def main():
    parser = argparse.ArgumentParser(
        prog="mcm-engine",
        description=(
            f"Memory Context Management engine for AI coding sessions "
            f"(v{__version__})"
        ),
    )
    parser.add_argument(
        "--version", "-v",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # run
    run_parser = subparsers.add_parser("run", help="Run the MCP server")
    run_parser.add_argument("--config", help="Path to mcm-engine.yaml")
    run_parser.add_argument("--project-root", help="Project root directory")
    run_parser.set_defaults(func=cmd_run)

    # init
    init_parser = subparsers.add_parser("init", help="Initialize mcm-engine config")
    init_parser.add_argument("--project", required=True, help="Project name")
    init_parser.add_argument("--project-root", help="Project root directory")
    init_parser.add_argument("--force", action="store_true", help="Overwrite existing config")
    init_parser.set_defaults(func=cmd_init)

    # serve (HTTP/SSE daemon)
    serve_parser = subparsers.add_parser(
        "serve",
        help="Run the engine as a long-lived HTTP/SSE daemon (MCM2-20)",
    )
    serve_parser.add_argument("--config", help="Path to mcm-engine.yaml")
    serve_parser.add_argument("--project-root", help="Project root directory")
    serve_parser.add_argument(
        "--host", default="127.0.0.1",
        help="Bind address (default 127.0.0.1; pass 0.0.0.0 for all interfaces).",
    )
    serve_parser.add_argument(
        "--port", type=int, default=8080,
        help="Bind port (default 8080).",
    )
    serve_parser.add_argument(
        "--transport", choices=["sse", "streamable-http"], default="sse",
        help="MCP transport variant (default sse).",
    )
    serve_parser.add_argument(
        "--allowed-host", action="append", default=[], metavar="HOST[:PORT]",
        help="Additional Host header value to accept past DNS-rebinding "
             "protection (repeatable). A bare host allows any port. Loopback "
             "is always allowed; when binding to 0.0.0.0 this machine's LAN "
             "IPs are auto-allowed. Name the address clients use (e.g. "
             "192.168.8.88) if auto-detection misses it.",
    )
    serve_parser.add_argument(
        "--no-dns-rebinding-protection", action="store_true",
        help="Disable Host/Origin validation entirely. Only for a trusted "
             "network; leaves the daemon open to DNS-rebinding attacks.",
    )
    serve_parser.set_defaults(func=cmd_serve)

    # migrate
    migrate_parser = subparsers.add_parser(
        "migrate",
        help="Copy storage between adapters (e.g., sqlite -> postgres)",
    )
    migrate_parser.add_argument(
        "--from", dest="source", required=True,
        help="Source DSN (e.g., sqlite:///path/to/db, postgresql://...)",
    )
    migrate_parser.add_argument(
        "--to", dest="dest", required=True,
        help="Destination DSN",
    )
    migrate_parser.add_argument(
        "--force", action="store_true",
        help="Allow writing to a non-empty destination (rows are appended; "
             "existing rows are not touched). Caller is responsible for "
             "truncating the destination if a clean slate is required.",
    )
    migrate_parser.set_defaults(func=cmd_migrate)

    # export-mirror (one-way DB -> git review mirror, issue #22)
    mirror_parser = subparsers.add_parser(
        "export-mirror",
        help="One-way DB->git review mirror of active rules (audit surface, "
             "never authoritative; read-only, never touches rules_paths).",
    )
    mirror_parser.add_argument(
        "--from", dest="source", required=True,
        help="Source storage DSN (sqlite:///path or postgresql://...).",
    )
    mirror_parser.add_argument(
        "--out", required=True,
        help="Output git directory (created and git-init'd if absent).",
    )
    mirror_parser.set_defaults(func=cmd_export_mirror)

    # consolidate (read-only KB-hygiene report, issue #31)
    consolidate_parser = subparsers.add_parser(
        "consolidate",
        help="Read-only KB-hygiene report: merge + conflict + stale candidates "
             "(propose-only; act via supersede_rule / archive / etc.).",
    )
    consolidate_parser.add_argument(
        "--from", dest="source", required=True,
        help="Source storage DSN (sqlite:///path or postgresql://...).",
    )
    consolidate_parser.add_argument(
        "--max-age-days", type=int, default=90,
        help="Age threshold (days) for stale candidates (default 90).",
    )
    consolidate_parser.set_defaults(func=cmd_consolidate)

    # mint-token (LODESTONE bearer token)
    mint_parser = subparsers.add_parser(
        "mint-token",
        help="Mint a bearer token for the HTTP/streamable-HTTP transport "
             "(requires postgres storage). Prints the plaintext token once.",
    )
    mint_parser.add_argument(
        "--principal", required=True,
        help="Logical owner of the token (free-form, e.g., 'paul', 'sieve', 'ci').",
    )
    mint_parser.add_argument("--config", help="Path to mcm-engine.yaml")
    mint_parser.add_argument("--project-root", help="Project root directory")
    mint_parser.set_defaults(func=cmd_mint_token)

    # hook (PreToolUse enforcement)
    hook_parser = subparsers.add_parser(
        "hook",
        help="PreToolUse enforcement hook for Claude Code / compatible agent harnesses. "
             "Reads one event from stdin; exits 0 (allow) or 2 (block).",
    )
    hook_parser.set_defaults(func=cmd_hook)

    # session-start (SessionStart context-injection hook)
    ss_parser = subparsers.add_parser(
        "session-start",
        help="SessionStart hook for Claude Code. Reads one event from stdin "
             "and prints resume context as additionalContext JSON. Always exits 0.",
    )
    ss_parser.set_defaults(func=cmd_session_start)

    # ingest (polymorphic bulk import)
    ingest_parser = subparsers.add_parser(
        "ingest",
        help="Bulk-ingest data from an external source (markdown dir today; "
             "more ingester types pluggable later) into the knowledge layer.",
        description="Reads a source path, detects its type (or uses --type), "
                    "and upserts one knowledge entry per record. Idempotent "
                    "on (topic, kind). Use --list-types to see what ingesters "
                    "are available.",
    )
    ingest_parser.add_argument(
        "source", nargs="?",
        help="Path or URL to ingest from (omit when using --list-types).",
    )
    ingest_parser.add_argument(
        "--type", dest="type", default=None,
        help="Force a specific ingester (skip auto-detection). "
             "See --list-types.",
    )
    ingest_parser.add_argument(
        "--list-types", action="store_true",
        help="Print registered ingester names and exit.",
    )
    ingest_parser.add_argument(
        "--kind", default="knowledge",
        help="KnowledgeRow.kind for ingested rows (default: knowledge).",
    )
    ingest_parser.add_argument(
        "--project", default=None,
        help="KnowledgeRow.project for ingested rows (default: none).",
    )
    ingest_parser.add_argument(
        "--skip", default=None,
        help="Comma-separated directory names to skip during traversal "
             "(ingester-specific; markdown-dir defaults to .obsidian,.trash,.git).",
    )
    ingest_parser.add_argument(
        "--bulk", action="store_true",
        help="Auto-insert every candidate as a knowledge row (skip the "
             "per-item evaluation step). Use only for declared-authoritative "
             "corpora; default mode is recommended for everything else.",
    )
    ingest_parser.add_argument(
        "--batch", type=int, default=25,
        help="Curated mode: how many candidates to emit per invocation "
             "(default 25). Ignored under --bulk.",
    )
    ingest_parser.add_argument(
        "--offset", type=int, default=0,
        help="Curated mode: start from this candidate index (default 0). "
             "Use to page through a large source in successive runs.",
    )
    ingest_parser.add_argument(
        "--dry-run", action="store_true",
        help="With --bulk, print what would be inserted/updated without "
             "writing. (Curated mode is already no-write.)",
    )
    ingest_parser.add_argument("--config", help="Path to mcm-engine.yaml")
    ingest_parser.add_argument("--project-root", help="Project root directory")
    ingest_parser.set_defaults(func=cmd_ingest)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
