"""MCM2-18 + MCM2-19: ``mcm-engine init`` produces a working config
that runs with NO external infrastructure (no Docker, no Postgres, no
Redis, no OpenSearch — embedded SQLite only).

This is the central guarantee for a clean-mac-mini install: type
``mcm-engine init --project foo`` and immediately have a working
knowledge engine. Tests pin the output shape AND verify the resulting
config can be loaded + wired without external services.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import yaml


def test_init_creates_expected_files(tmp_path):
    """init writes mcm-engine.yaml, .claude/, rules/."""
    proc = subprocess.run(
        [sys.executable, "-m", "mcm_engine.cli", "init",
         "--project", "test-proj",
         "--project-root", str(tmp_path)],
        capture_output=True, text=True, check=True,
    )

    assert (tmp_path / "mcm-engine.yaml").exists()
    assert (tmp_path / ".claude").is_dir()
    assert (tmp_path / "rules").is_dir()
    assert "Created" in proc.stdout


def test_init_config_has_no_backends_block(tmp_path):
    """Default init MUST NOT pin any external backend — the engine
    auto-resolves embedded SQLite for everything when no `backends:`
    block exists. Adding one to the default would force users to
    install psycopg / redis / opensearch-py just to type `init`."""
    subprocess.run(
        [sys.executable, "-m", "mcm_engine.cli", "init",
         "--project", "test-proj",
         "--project-root", str(tmp_path)],
        capture_output=True, text=True, check=True,
    )

    config = yaml.safe_load((tmp_path / "mcm-engine.yaml").read_text())
    assert "backends" not in config
    # No external-infra-implying keys.
    assert "postgres_dsn" not in config
    assert "redis_url" not in config


def test_init_config_loads_and_wires_against_embedded(tmp_path):
    """The init-produced config loads via load_config() and the
    wiring layer produces a Context with all four embedded adapters
    populated. No external infra touched."""
    from mcm_engine.config import load_config
    from mcm_engine.wiring import build_context

    subprocess.run(
        [sys.executable, "-m", "mcm_engine.cli", "init",
         "--project", "test-proj",
         "--project-root", str(tmp_path)],
        capture_output=True, text=True, check=True,
    )

    config = load_config(
        config_path=tmp_path / "mcm-engine.yaml",
        project_root=tmp_path,
    )
    assert config.project_name == "test-proj"

    ctx = build_context(config)
    assert ctx.storage is not None
    assert ctx.counters is not None
    assert ctx.search is not None
    assert ctx.session is not None

    # The resolved classes MUST be the embedded SQLite/in-memory set.
    from mcm_engine.adapters.sqlite.counters import SqliteCounters
    from mcm_engine.adapters.sqlite.search import SqliteSearch
    from mcm_engine.adapters.sqlite.session import InMemorySession
    from mcm_engine.adapters.sqlite.storage import SqliteStorage

    assert isinstance(ctx.storage, SqliteStorage)
    assert isinstance(ctx.counters, SqliteCounters)
    assert isinstance(ctx.search, SqliteSearch)
    assert isinstance(ctx.session, InMemorySession)


def test_init_force_overwrites(tmp_path):
    (tmp_path / "mcm-engine.yaml").write_text("project_name: stale\n")
    subprocess.run(
        [sys.executable, "-m", "mcm_engine.cli", "init",
         "--project", "fresh",
         "--project-root", str(tmp_path),
         "--force"],
        capture_output=True, text=True, check=True,
    )
    config = yaml.safe_load((tmp_path / "mcm-engine.yaml").read_text())
    assert config["project_name"] == "fresh"


def test_init_refuses_existing_without_force(tmp_path):
    (tmp_path / "mcm-engine.yaml").write_text("project_name: existing\n")
    proc = subprocess.run(
        [sys.executable, "-m", "mcm_engine.cli", "init",
         "--project", "fresh",
         "--project-root", str(tmp_path)],
        capture_output=True, text=True,
    )
    assert proc.returncode != 0
    assert "already exists" in proc.stderr
