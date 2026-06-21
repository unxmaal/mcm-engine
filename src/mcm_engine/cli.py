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
    from .transport import serve

    config_path = Path(args.config) if args.config else None
    project_root = Path(args.project_root) if args.project_root else None

    config = load_config(config_path=config_path, project_root=project_root)
    server = MCMServer(config, project_root=project_root or Path.cwd())
    serve(server, host=args.host, port=args.port, transport=args.transport)


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


def cmd_hook(args):
    """Run the PreToolUse enforcement hook. Reads a single event from
    stdin and exits 0 (allow) or 2 (block). Wire into your agent
    harness's settings.json — see README "Making agents actually use it"
    section."""
    from .hooks.mcp_enforcement import main as hook_main
    sys.exit(hook_main())


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

    # hook (PreToolUse enforcement)
    hook_parser = subparsers.add_parser(
        "hook",
        help="PreToolUse enforcement hook for Claude Code / compatible agent harnesses. "
             "Reads one event from stdin; exits 0 (allow) or 2 (block).",
    )
    hook_parser.set_defaults(func=cmd_hook)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
