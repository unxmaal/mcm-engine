"""Issue #30: deterministic MinHash/LSH near-duplicate detection for rules.

Embedding-free, deterministic (fixed hash permutations -> same corpus, same
clusters across runs). Surfacing only: nothing here mutates the store.
"""
import pytest

from mcm_engine.config import NudgeConfig
from mcm_engine.dedup import (
    find_near_duplicates,
    jaccard_estimate,
    normalize,
    shannon_entropy,
)
from mcm_engine.tracker import SessionTracker
from mcm_engine.tools.rules import register_rules_tools

LONG_A = ("the postgres adapter scores search results with ts rank cd which "
          "is a much smaller magnitude than sqlite fts five bm twenty five so "
          "a fixed sigmoid midpoint collapses relevance on the pod")
# one-word minor edit near the end (relevance -> ranking)
LONG_B = ("the postgres adapter scores search results with ts rank cd which "
          "is a much smaller magnitude than sqlite fts five bm twenty five so "
          "a fixed sigmoid midpoint collapses ranking on the pod")
DISTINCT = ("cranelift regalloc two produces occasional miscompilations in "
            "blocks with multiple helper call diamonds so all store free "
            "blocks must be speculative to enable snapshot rollback demotion")


# --- primitives -------------------------------------------------------------

def test_normalize_strips_punct_and_case():
    assert normalize("Foo, Bar!  BAZ") == "foo bar baz"


def test_identical_after_normalize_is_jaccard_one():
    assert jaccard_estimate("Rule About Xyz!", "rule about xyz") == pytest.approx(1.0)


def test_minor_edit_is_high_similarity():
    assert jaccard_estimate(LONG_A, LONG_B) >= 0.8


def test_distinct_texts_are_low_similarity():
    assert jaccard_estimate(LONG_A, DISTINCT) < 0.3


def test_shannon_entropy_gate_values():
    assert shannon_entropy("a a a") < 1.5
    assert shannon_entropy(LONG_A) >= 1.5


# --- clustering -------------------------------------------------------------

def test_clusters_near_dups_not_distinct():
    items = [(1, LONG_A), (2, LONG_B), (3, DISTINCT)]
    clusters = find_near_duplicates(items, threshold=0.8)
    assert any(set(c) == {1, 2} for c in clusters)
    assert all(3 not in c for c in clusters)


def test_exact_normalized_duplicate_clustered_at_strict_threshold():
    items = [(1, LONG_A.upper() + "!!!"), (2, LONG_A), (3, DISTINCT)]
    clusters = find_near_duplicates(items, threshold=0.9)
    assert any(set(c) == {1, 2} for c in clusters)


def test_low_entropy_items_are_gated_not_merged():
    # trivially low-information text is excluded from clustering even if identical
    items = [(1, "a a a"), (2, "a a a"), (3, LONG_A)]
    clusters = find_near_duplicates(items, threshold=0.9)
    assert all(1 not in c and 2 not in c for c in clusters)


def test_determinism_same_input_same_clusters():
    items = [(1, LONG_A), (2, LONG_B), (3, DISTINCT), (4, LONG_A)]
    r1 = find_near_duplicates(items, threshold=0.8)
    r2 = find_near_duplicates(items, threshold=0.8)
    assert r1 == r2


# --- surfacing tool (read-only) --------------------------------------------

class FakeMCP:
    def __init__(self):
        self._tools = {}

    def tool(self):
        def deco(fn):
            self._tools[fn.__name__] = fn
            return fn
        return deco

    def __getitem__(self, name):
        return self._tools[name]


@pytest.fixture
def rules_env(db, project_root):
    mcp = FakeMCP()
    tracker = SessionTracker(NudgeConfig(store_reminder_turns=100,
                                         checkpoint_turns=100,
                                         mandatory_stop_turns=200))
    register_rules_tools(mcp, db, tracker, "test-project",
                         [project_root / "rules"], project_root)
    from mcm_engine.adapters.sqlite.storage import SqliteStorage
    return mcp, SqliteStorage(db=db)


def test_find_duplicate_rules_surfaces_pair_and_mutates_nothing(rules_env):
    mcp, storage = rules_env
    mcp["add_rule"](title="Postgres ranking scale", keywords="postgres ranking",
                    content=LONG_A)
    mcp["add_rule"](title="Postgres ranking scale v2", keywords="postgres ranking",
                    content=LONG_A)  # same content, different title -> near-dup
    mcp["add_rule"](title="JIT speculative", keywords="jit", content=DISTINCT)

    from mcm_engine.backends import EntityType
    before = len(list(storage.iter_entries(EntityType.RULE)))

    out = mcp["find_duplicate_rules"](threshold=0.8)

    assert "Postgres ranking scale" in out
    assert "JIT speculative" not in out  # the distinct rule is not surfaced
    after = len(list(storage.iter_entries(EntityType.RULE)))
    assert before == after == 3  # read-only: nothing merged/deleted
