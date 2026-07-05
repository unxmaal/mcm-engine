"""Regression guard on the MCM2-01 seam inventory.

The inventory at docs/seam-inventory.md is a complete catalog of every SQL
execution site in src/mcm_engine/. Phase 0 work depends on it staying
accurate. If new SQL is added (or old SQL removed) without updating the
inventory, this test fails — forcing the inventory to stay in sync.

How to update when the test fails: update the EXPECTED_* constants below
AND the corresponding section of docs/seam-inventory.md so the two stay
locked together.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src" / "mcm_engine"
INVENTORY = REPO_ROOT / "docs" / "seam-inventory.md"


# Files known to contain SQL execution sites per the inventory.
# Counts are total .execute() / .execute_write() / .executescript() /
# .executemany() call sites in each file. PRAGMA calls in db.py count.
#
# These numbers are the ground truth as of MCM2-01. When the test fails,
# either update the inventory and these numbers together, or rip out the
# new SQL.
EXPECTED_SQL_SITES_BY_FILE: dict[str, int] = {
    # Original v1 surface — tools still hold their SQL directly until the
    # tool-side refactor lands (a follow-up step of MCM2-02).
    "db.py":               7,
    "schema.py":           56,  # +7 issue #21 v8→v9; +1 issue #37 v9→v10 token_ledger CREATE
    "plugin.py":           0,   # MCM2-07 — SearchScope.search SQL moved to SqliteSearch.search_plugin
    "tools/search.py":     0,   # MCM2-02 rewire complete (composite rank in scoring.py)
    "tools/knowledge.py":  3,   # MCM2-02 rewire complete — uses ctx adapters. +3 for LODESTONE kb_recall (SELECT/INSERT/DELETE recall path; single-store, no adapter abstraction warranted).
    "tools/rules.py":      0,   # MCM2-02 rewire complete
    "tools/relations.py":  0,   # MCM2-02 rewire complete
    "tools/session.py":    0,   # MCM2-02 rewire complete
    # MCM2-02 embedded SQLite adapter — SQL extracted out of tools into the
    # repository. These files are the new authoritative home for SQL.
    "adapters/sqlite/storage.py":  56,  # +5 #21; +2 #37 (token ledger); +1 #36 (list_rule_outcomes SELECT); +1 #54 (find_rule_by_content_hash)
    "adapters/sqlite/search.py":   5,   # +2 for MCM2-07 search_plugin (FTS + LIKE)
    "adapters/sqlite/counters.py": 4,
    # MCM2-08 Postgres adapter — first non-embedded reference. SQL count
    # matches SqliteStorage's contract surface minus the FTS-table reads
    # (Postgres folds FTS into the same row via tsvector generated columns).
    # +7 MCM2-11 id-preserving inserts, +4 iter, +1 bump_sequences.
    "adapters/postgres/storage.py": 57,  # +5 #21; +2 #37 (token ledger); +1 #36 (list_rule_outcomes SELECT); +1 #54 (find_rule_by_content_hash)
    # MCM2-13b: PostgresCounters (write-through to entry rows, mirrors SqliteCounters shape).
    "adapters/postgres/counters.py": 5,
    # MCM2-15a: PostgresSearch (tsvector + ts_rank_cd, LIKE fallback,
    # plugin search via ILIKE).
    "adapters/postgres/search.py": 4,
    # LODESTONE additive surface (see lodestone-lite-plan.md).
    # tokens.py: mint INSERT, validate SELECT + UPDATE-touch, revoke UPDATE.
    "tokens.py": 4,
    # transport.py: /v1/claims INSERT into knowledge.
    "transport.py": 1,
}


SQL_EXEC_RE = re.compile(r"\.(execute|execute_write|executescript|executemany)\s*\(")


def _count_sql_sites(path: Path) -> int:
    if not path.exists():
        return 0
    return len(SQL_EXEC_RE.findall(path.read_text(encoding="utf-8")))


def test_inventory_file_exists():
    """The seam inventory must exist. MCM2-01 produces it."""
    assert INVENTORY.exists(), (
        f"docs/seam-inventory.md is missing. MCM2-01 says it must exist; "
        f"re-create it before any other Phase 0 work."
    )


def test_inventory_file_non_trivial():
    """The seam inventory must be substantive, not a stub."""
    content = INVENTORY.read_text(encoding="utf-8")
    # Sanity threshold: a real inventory has >100 lines and mentions every tool file.
    assert content.count("\n") > 100, "Inventory is too short — likely a stub."
    for tool_file in ["search.py", "knowledge.py", "rules.py", "relations.py", "session.py"]:
        assert tool_file in content, (
            f"Inventory does not mention {tool_file}; it should be cataloged."
        )


@pytest.mark.parametrize("rel_path,expected", sorted(EXPECTED_SQL_SITES_BY_FILE.items()))
def test_sql_site_count_per_file(rel_path: str, expected: int):
    """Each cataloged file's SQL-execution-site count is pinned to its
    inventory state.

    A new .execute() or .execute_write() call appearing in src/mcm_engine/
    without a corresponding inventory update will trip this test, by
    design.
    """
    actual = _count_sql_sites(SRC_DIR / rel_path)
    assert actual == expected, (
        f"\n  SQL site count drifted in src/mcm_engine/{rel_path}\n"
        f"  Expected (per inventory): {expected}\n"
        f"  Actual (in source):       {actual}\n"
        f"  Update docs/seam-inventory.md AND EXPECTED_SQL_SITES_BY_FILE together."
    )


def test_no_sql_in_unexpected_files():
    """No SQL execution outside the cataloged files in src/mcm_engine/.

    If this fails, either (a) new code introduced SQL where none was
    expected, or (b) the inventory missed a site. Either way, update both.
    """
    cataloged = set(EXPECTED_SQL_SITES_BY_FILE)
    surprises: list[tuple[str, int]] = []
    for py_file in sorted(SRC_DIR.rglob("*.py")):
        rel = str(py_file.relative_to(SRC_DIR))
        if rel in cataloged:
            continue
        n = _count_sql_sites(py_file)
        if n > 0:
            surprises.append((rel, n))
    assert not surprises, (
        "\n  Uncataloged SQL found in src/mcm_engine/:\n"
        + "\n".join(f"    {p}: {n} site(s)" for p, n in surprises)
        + "\n  Add to inventory and EXPECTED_SQL_SITES_BY_FILE, or remove the SQL."
    )
