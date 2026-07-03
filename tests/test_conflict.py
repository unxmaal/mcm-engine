"""Issue #32 — automatic contradiction detection (surfacing only).

A CONFLICT candidate is a pair of rules that are TOPICALLY similar (title +
keywords overlap high) but whose BODIES diverge (content overlap low) — "same
subject, opposite story". That is the inverse of a #30 near-duplicate (where
BOTH are high). Detection is deterministic and embedding-free; it surfaces
candidates for the existing `supersede_rule` — it never auto-supersedes and
puts no LLM in the write path.
"""
from __future__ import annotations

from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, RuleRow
from mcm_engine.config import NudgeConfig
from mcm_engine.db import KnowledgeDB
from mcm_engine.dedup import find_conflicts
from mcm_engine.schema import migrate_core
from mcm_engine.tracker import SessionTracker
from mcm_engine.tools.rules import register_rules_tools


# --- find_conflicts unit tests ---------------------------------------------

def test_same_topic_divergent_body_is_a_conflict():
    items = [
        (1, "cache invalidation strategy policy",
         "always invalidate immediately on every write with no exceptions ever"),
        (2, "cache invalidation strategy policy",
         "never invalidate eagerly rely only on ttl expiry timers instead"),
    ]
    pairs = find_conflicts(items)
    assert any(a == 1 and b == 2 for a, b, _ in pairs)
    # neither body contains the other -> contradictory (issue #33 typing)
    assert next(lab for a, b, lab in pairs if (a, b) == (1, 2)) == "contradictory"


def test_near_duplicate_same_body_is_not_a_conflict():
    items = [
        (1, "logging format standard convention",
         "use structured json logs with iso timestamps and severity levels"),
        (2, "logging format standard convention",
         "use structured json logs with iso timestamps and severity levels always"),
    ]
    assert find_conflicts(items) == []


def test_unrelated_topics_not_a_conflict():
    items = [
        (1, "cache invalidation strategy policy",
         "always invalidate immediately on every write"),
        (2, "database migration transaction safety wrapping",
         "wrap every migration in a single atomic transaction"),
    ]
    assert find_conflicts(items) == []


def test_empty_body_pair_is_not_flagged():
    # can't assess body divergence with no bodies
    items = [
        (1, "cache invalidation strategy policy", ""),
        (2, "cache invalidation strategy policy", ""),
    ]
    assert find_conflicts(items) == []


def test_deterministic():
    items = [
        (1, "cache invalidation strategy policy",
         "always invalidate immediately on every write no exceptions"),
        (2, "cache invalidation strategy policy",
         "never invalidate eagerly rely only on ttl expiry timers"),
        (3, "unrelated widget coloring behavior thing",
         "widgets pick their color from the parent theme at render time"),
    ]
    assert find_conflicts(items) == find_conflicts(items)


# --- #33 conflict typing ----------------------------------------------------

def test_classify_conflict_subsumes_subsumed_contradictory():
    from mcm_engine.dedup import classify_conflict

    big = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda"
    small = "alpha beta gamma delta"                       # subset of big
    disjoint = "one two three four five six seven eight"
    assert classify_conflict(big, small) == "subsumes"
    assert classify_conflict(small, big) == "subsumed"
    assert classify_conflict(big, disjoint) == "contradictory"


def test_find_conflicts_labels_subsumption():
    # Same topic; #2's body is a small subset of #1's large body -> low overall
    # similarity (a conflict) but full containment -> labeled 'subsumes'.
    big = ("retry policy exponential backoff jitter max attempts five base delay "
           "hundred milliseconds circuit breaker threshold ten failures window "
           "sixty seconds half open probe single request timeout two seconds")
    small = "retry policy exponential backoff jitter max attempts"
    items = [(1, "retry policy configuration", big),
             (2, "retry policy configuration", small)]
    labeled = {(a, b): lab for a, b, lab in find_conflicts(items)}
    assert labeled.get((1, 2)) == "subsumes"


# --- tool + add_rule wiring -------------------------------------------------

class _FakeMCP:
    def __init__(self):
        self._tools = {}

    def tool(self):
        def deco(fn):
            self._tools[fn.__name__] = fn
            return fn
        return deco

    def __getitem__(self, name):
        return self._tools[name]


def _wire(tmp_path):
    db = KnowledgeDB(tmp_path / "r.db")
    migrate_core(db)
    rules_dir = tmp_path / "rules"
    rules_dir.mkdir()
    mcp = _FakeMCP()
    tracker = SessionTracker(NudgeConfig(
        store_reminder_turns=1000, checkpoint_turns=1000, mandatory_stop_turns=100000,
    ))
    register_rules_tools(mcp, db, tracker, "t", [rules_dir], tmp_path,
                         files_authoritative=False)
    return mcp, SqliteStorage(db=db)


def test_find_conflicting_rules_surfaces_pair_and_mutates_nothing(tmp_path):
    mcp, storage = _wire(tmp_path)
    a = storage.insert_rule(RuleRow(id=0, title="cache invalidation strategy policy",
                                    keywords="cache invalidation",
                                    content="always invalidate immediately on every write"))
    b = storage.insert_rule(RuleRow(id=0, title="cache invalidation strategy policy",
                                    keywords="cache invalidation",
                                    content="never invalidate eagerly rely on ttl expiry only"))
    storage.insert_rule(RuleRow(id=0, title="database migration transaction safety wrapping",
                                keywords="database migration",
                                content="wrap every migration in a single atomic transaction"))
    before = len(list(storage.iter_entries(EntityType.RULE)))

    out = mcp["find_conflicting_rules"]()

    assert f"#{a}" in out and f"#{b}" in out
    after = len(list(storage.iter_entries(EntityType.RULE)))
    assert after == before == 3   # read-only


def test_add_rule_surfaces_conflict_note(tmp_path):
    # Distinct titles (else add_rule's title-dedup would UPDATE, not create a
    # second rule), but near-identical topic via shared keywords -> topically
    # similar; bodies diverge -> a conflict candidate.
    mcp, _ = _wire(tmp_path)
    kw = "cache invalidation write ttl policy strategy"
    mcp["add_rule"](title="cache invalidation timing", keywords=kw,
                    content="always invalidate immediately on every write with no delay")
    out = mcp["add_rule"](title="cache invalidation timing rules", keywords=kw,
                          content="never invalidate eagerly rely only on ttl expiry timers")
    assert "conflict" in out.lower()
    assert "supersede_rule" in out


def test_add_rule_no_conflict_note_when_unrelated(tmp_path):
    mcp, _ = _wire(tmp_path)
    mcp["add_rule"](title="cache invalidation strategy policy", keywords="cache",
                    content="always invalidate immediately on write")
    out = mcp["add_rule"](title="database migration transaction safety wrapping",
                          keywords="database", content="wrap migrations in a transaction")
    assert "conflict" not in out.lower()
