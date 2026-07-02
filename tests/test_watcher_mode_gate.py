"""Issue #16 — Layer 1: the startup watcher is gated on source_of_truth.

In `database` mode the DB is authoritative; the startup file->DB sync and the
observer must not run, so a pod restart with an empty/absent rules dir can no
longer archive DB-native rules. Includes the headline 177-wipe repro.
"""
from __future__ import annotations

import pytest

from mcm_engine.backends import RuleRow
from mcm_engine.config import MCMConfig
from mcm_engine.server import MCMServer


def _server(tmp_path, mode: str) -> MCMServer:
    cfg = MCMConfig(
        project_name="gate-test",
        db_path=str(tmp_path / "gate.db"),
        source_of_truth=mode,
    )
    return MCMServer(cfg, project_root=tmp_path)


def _spy_watcher(server):
    calls = {"sync_once": 0, "start": 0}

    def sync_once():
        calls["sync_once"] += 1
        return {"upserted": 0, "archived": 0, "unchanged": 0, "links": 0,
                "archive_blocked": 0}

    def start():
        calls["start"] += 1

    server.watcher.sync_once = sync_once
    server.watcher.start = start
    return calls


def test_database_mode_start_watcher_is_noop(tmp_path):
    server = _server(tmp_path, "database")
    calls = _spy_watcher(server)
    server.start_watcher()
    assert calls == {"sync_once": 0, "start": 0}


def test_files_mode_start_watcher_runs(tmp_path):
    server = _server(tmp_path, "files")
    calls = _spy_watcher(server)
    server.start_watcher()
    assert calls["sync_once"] == 1
    assert calls["start"] == 1


def test_177_repro_database_mode_restart_preserves_rules(tmp_path):
    """The headline incident: rules pushed into a DB-authoritative pod (each
    carrying a provenance file_path from the loader), then a restart. With the
    mode gate, the startup watcher never runs, so nothing is archived."""
    server = _server(tmp_path, "database")
    storage = server.ctx.storage

    # Simulate import_rules loading 177 rules with provenance file_paths that
    # happen to sit under the (empty) rules dir — the exact shape that wiped.
    (tmp_path / "rules").mkdir(exist_ok=True)
    for i in range(177):
        storage.insert_rule(RuleRow(
            id=0, title=f"pushed-{i}", keywords="kw",
            file_path=f"rules/pushed-{i}.md", content=f"body {i}"))

    # "Restart": the daemon startup path.
    server.start_watcher()

    survivors = [r for r in storage.list_rules_with_file_paths() if not r.archived]
    assert len(survivors) == 177
    assert storage.find_rule_by_title("pushed-0").archived is False
