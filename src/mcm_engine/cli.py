"""CLI entry point: mcm-engine run / mcm-engine init."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from .config import MCMConfig, load_config
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


def main():
    parser = argparse.ArgumentParser(
        prog="mcm-engine",
        description="Memory Context Management engine for AI coding sessions",
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

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
