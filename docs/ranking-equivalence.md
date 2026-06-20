# Ranking equivalence: SQLite FTS5 vs Postgres tsvector

This document explains why two `SearchBackend` adapters can return
**different orderings** for the same query and still both be correct, and
why the engine's composite score (`mcm_engine.scoring.compose_rank`) tolerates
that variance.

## The short version

- SQLite FTS5's `rank` is **bm25** scaled negative-better.
- Postgres `ts_rank_cd` is **a cover-density score** scaled positive-better.
- Different primitives, different magnitudes, different orderings on
  borderline matches.
- Both adapters normalize to **higher = better** at the `SearchBackend`
  boundary. The composite scorer then mixes lexical score with
  hit-count, reinforcement, pin, and recency to produce the final
  ranking the user sees.
- **Result orderings between adapters are not bit-identical and are
  not expected to be.** What the contract guarantees is that the
  *first* result is the *best* lexical-or-better match per the
  composite, and that pinned items always outrank unpinned ones.

If you need bit-equivalent ordering across adapters, you want a
test bug, not a feature.

## What each primitive actually does

### SQLite FTS5 `rank`

FTS5 ships an implementation of bm25 as the default ranking function.
Score components:
- Term frequency in the document.
- Inverse document frequency across the corpus.
- Document length normalization (k1=1.2, b=0.75 by default).
- Column weights (we don't customize; the per-column setweight analogue
  is `bm25(table, w_col1, w_col2, ...)` which we don't use).

Returned value is **negative**, with lower (more negative) = better.

### Postgres `ts_rank_cd`

`ts_rank_cd(tsvector, tsquery, normalization)` returns a non-negative
cover-density score. Components:
- How close the query terms cluster (cover density).
- Weight class of the matched lexemes (A > B > C > D, set via
  `setweight()` in the tsvector definition).
- Optional length normalization via the third argument (we leave it at
  default = 0, which means no length normalization).

We use it with the engine's setweight scheme (see
`docs/schema-migration-v6-to-postgres.md`): `topic`/`title` → A,
`summary`/`keywords` → B, `detail`/`description` → C, `tags`/`category` → D.

Returned value is **non-negative**, higher = better.

## Why the orderings differ

bm25 and cover-density disagree most on:

1. **Multi-term queries with one rare term.** bm25 weighs the rare term
   highly via IDF; cover-density cares more that terms are *clustered*.
   The same row can be #1 under bm25 and #5 under ts_rank_cd.
2. **Long documents.** bm25 explicitly down-weights long documents via
   `b`; ts_rank_cd at normalization=0 does not. A 5000-char detail will
   rank lower in SQLite than in Postgres for the same hit pattern.
3. **Stemming edge cases.** SQLite's `porter unicode61` and Postgres's
   `english` config both use Snowball Porter, but their tokenizer
   pre-processing differs slightly (Unicode normalization, punctuation
   handling). A query like `"don't"` tokenizes differently across the
   two and can match a document under one adapter while missing it
   under the other.

These differences are **the cost of being adapter-agnostic**. The plan
explicitly accepts this (NG-3: tests assert shape, not ordering).

## How the engine handles it

`mcm_engine.scoring.compose_rank` takes the lexical score and folds in:

```python
composite = raw_rank
          + HIT_WEIGHT          * hit_count
          + REINFORCEMENT_WEIGHT * reinforcement_count
          + PINNED_WEIGHT       * (1 if pinned else 0)
          + recency_bonus(age_days)
```

With `PINNED_WEIGHT = 2.0` and `RECENCY_WINDOW_DAYS = 30`, the
behavioral counters are dominant within ~10 hits. The lexical score
acts more as a tie-breaker among matches than as the primary signal.

**Implication:** as long as both adapters normalize to higher-better and
both return *the same set of relevant rows* (i.e. don't silently miss
matches), the composite stays stable. The ordering within the top-N is
allowed to vary; the cardinality of "relevant" stays the same.

## Sign normalization at the boundary

This is locked in by a persistent rule (see
`rules/mcm2/search-adapter-contract-normalize-composite-rank-sign-at-the-boundary.md`):

- `SqliteSearch` flips FTS5's negative rank: `score = -float(raw_rank)`.
- `PostgresSearch` (when implemented in Phase 3) returns `ts_rank_cd`
  directly.
- LIKE fallback (no rank) uses a baseline `0.5`.

Both produce `SearchHit.score` with higher = better. The composite
scorer assumes this throughout — see `mcm_engine/scoring.py`.

## What this means for tests

The shared `SearchConformance` suite at
`mcm_engine.testing.conformance.SearchConformance` asserts:

- A match for a clearly-relevant query returns ≥1 hit.
- `SearchHit.score > 0` (higher-better normalization).
- Two seeded matches return in score-descending order.
- The pinned flag propagates regardless of score.
- An unknown query returns `[]`.

It does **not** assert specific score magnitudes, specific orderings of
nearly-tied matches, or behavior on stemming-edge inputs. The Postgres
adapter and the SQLite adapter both pass this suite without contortion.

## Future: rank-equivalence test

When we ship `PostgresSearch`, we'll add an *advisory* test that runs the
same seeded fixture through both adapters and reports the top-3 set
overlap. Expectation: ≥2 of 3 rows match for "obviously relevant"
queries. Drift below that is a real signal to investigate; bit-identical
ordering is not.
