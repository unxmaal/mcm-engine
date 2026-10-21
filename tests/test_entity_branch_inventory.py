"""Regression guard on EntityType dispatch sites (issue #178, 4.0 program #176).

The four-way entity split forks the code at every `is EntityType.X` /
`== EntityType.X` / `in (EntityType.…` site. The unified-entries migration
must MOVE this number: each child lowers the pins as it collapses a fork, and a
new fork outside the sanctioned policy module fails here, the way
test_seam_inventory.py pins SQL sites.

Classification (the umbrella's inventory): `incidental` collapses into the
unified model (hydrate-by-type, formatter dispatch, copy-by-type, label by
type, projection by type); `essential` is a genuine kind policy (rule-only
governance, which kinds track hit counts); `test` is a conformance assertion
on a hit's entity_type.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src" / "mcm_engine"

DISPATCH_RE = re.compile(r"\bis EntityType\.|== EntityType\.|\bin \(EntityType\.")

# The only module allowed to branch on kind once #181 lands. Sites here are
# not counted against the pins.
POLICY_MODULES = frozenset({"policies.py"})

# file -> (pinned count, classification note)
EXPECTED_DISPATCH_SITES: dict[str, tuple[int, str]] = {
    "tools/search.py":              (8, "incidental x4 formatter dispatch; essential x1 hit-count kinds; essential x3 rule-only related spreading"),
    "adapters/sqlite/storage.py":   (8, "incidental: hydrate-by-type in list + find_by_id"),
    "adapters/postgres/storage.py": (8, "incidental: hydrate-by-type in list + find_by_id"),
    "tools/relations.py":           (4, "incidental: label by type"),
    "tools/rules.py":               (3, "incidental: promote_to_rule projection by type"),
    "testing/conformance.py":       (3, "test: hit.entity_type assertions"),
    "admin/service.py":             (2, "essential: rule-graph relation filter"),
    "corpus_snapshot.py":           (7, "incidental: #177 parse/project by type (sanctioned legacy-shape tool, retired with the legacy tables)"),
    "tools/corpus.py":              (6, "incidental: #170 sanitize plan/apply by type (collapses into the unified text-field map)"),
}
EXPECTED_TOTAL = 49  # #180: migrate.py's copy-by-type collapsed into entries.insert_legacy


def _count(path: Path) -> int:
    return len(DISPATCH_RE.findall(path.read_text(encoding="utf-8")))


def _all_sites() -> dict[str, int]:
    out: dict[str, int] = {}
    for p in sorted(SRC_DIR.rglob("*.py")):
        if "__pycache__" in p.parts or p.name in POLICY_MODULES:
            continue
        n = _count(p)
        if n:
            out[str(p.relative_to(SRC_DIR))] = n
    return out


@pytest.mark.parametrize("rel_path,expected", sorted(
    (k, v[0]) for k, v in EXPECTED_DISPATCH_SITES.items()))
def test_dispatch_site_count_per_file(rel_path: str, expected: int):
    actual = _count(SRC_DIR / rel_path)
    assert actual == expected, (
        f"\n  EntityType dispatch sites drifted in src/mcm_engine/{rel_path}\n"
        f"  Expected (pinned): {expected}\n  Actual: {actual}\n"
        f"  A fork collapsed? lower the pin. A new fork? it belongs in a kind "
        f"policy (POLICY_MODULES), not here."
    )


def test_no_dispatch_in_unpinned_files():
    unexpected = {k: v for k, v in _all_sites().items() if k not in EXPECTED_DISPATCH_SITES}
    assert not unexpected, (
        f"EntityType dispatch appeared in files not in the inventory: {unexpected}. "
        f"Branch on kind only inside a policy module."
    )


def test_total_dispatch_sites_pinned():
    total = sum(_all_sites().values())
    assert total == EXPECTED_TOTAL, (
        f"total EntityType dispatch sites {total} != pinned {EXPECTED_TOTAL}; "
        f"update the per-file pins and this total together."
    )
