"""Deterministic near-duplicate detection for rules (issue #30, gap 4).

Embedding-free MinHash + LSH banding over rule text, in the spirit of
Graphiti's `dedup_helpers.py`. The pre-#30 KB had only title-string-equality
dedup, so near-identical rules (same content, cosmetic diff; or a re-store with
a minor edit) accumulated silently.

DETERMINISTIC by construction: the MinHash permutations are fixed blake2b hashes
keyed by a constant per-slot seed, so the same corpus yields the same clusters
across runs and machines. That is the whole point versus an embedding/RAG
approach — reproducible, auditable candidate detection with no model and no RNG.

SURFACING ONLY: this module finds candidate clusters; it never mutates a store.
Callers (e.g. the `find_duplicate_rules` tool) decide what, if anything, to
supersede.
"""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from typing import Iterable

_WORD_RE = re.compile(r"[a-z0-9]+")

NUM_PERM = 32          # MinHash signature length
SHINGLE_N = 2          # word n-gram size (bigrams: order-aware but lenient)
LSH_BANDS = 8          # NUM_PERM must be divisible by LSH_BANDS
MIN_ENTROPY = 1.5      # Shannon entropy gate: skip trivially low-information text
_MAX_HASH = (1 << 64) - 1


def normalize(text: str) -> str:
    """Lowercase, drop non-alphanumerics to spaces, collapse whitespace."""
    return " ".join(_WORD_RE.findall((text or "").lower()))


def _tokens(text: str) -> list[str]:
    return normalize(text).split()


def shannon_entropy(text: str) -> float:
    """Token-distribution Shannon entropy (bits). Low for trivial/repetitive
    text, so it gates out titles too low-information to dedup reliably."""
    toks = _tokens(text)
    if not toks:
        return 0.0
    total = len(toks)
    counts = Counter(toks)
    return -sum((c / total) * math.log2(c / total) for c in counts.values())


def _shingles(text: str, n: int = SHINGLE_N) -> frozenset[str]:
    toks = _tokens(text)
    if len(toks) < n:
        return frozenset(toks)  # too short for n-grams: fall back to token set
    return frozenset(" ".join(toks[i:i + n]) for i in range(len(toks) - n + 1))


def _perm_hash(shingle: str, seed: int) -> int:
    """Deterministic per-permutation hash: blake2b keyed by a fixed seed."""
    digest = hashlib.blake2b(
        shingle.encode("utf-8"), digest_size=8, salt=seed.to_bytes(2, "big")
    ).digest()
    return int.from_bytes(digest, "big")


def minhash_signature(text: str, num_perm: int = NUM_PERM) -> tuple[int, ...]:
    """Fixed-permutation MinHash signature of the text's shingle set."""
    sh = _shingles(text)
    if not sh:
        return tuple([_MAX_HASH] * num_perm)
    return tuple(min(_perm_hash(s, seed) for s in sh) for seed in range(num_perm))


def _signature_similarity(a: tuple[int, ...], b: tuple[int, ...]) -> float:
    if not a or not b:
        return 0.0
    return sum(1 for x, y in zip(a, b) if x == y) / len(a)


def jaccard_estimate(a_text: str, b_text: str, num_perm: int = NUM_PERM) -> float:
    """MinHash estimate of the Jaccard similarity of two texts' shingle sets."""
    return _signature_similarity(
        minhash_signature(a_text, num_perm), minhash_signature(b_text, num_perm)
    )


class _UnionFind:
    def __init__(self) -> None:
        self._parent: dict = {}

    def find(self, x):
        p = self._parent.setdefault(x, x)
        while p != x:
            self._parent[x] = self._parent.setdefault(p, p)
            x, p = p, self._parent[p]
        return x

    def union(self, a, b) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self._parent[ra] = rb


def find_near_duplicates(
    items: Iterable[tuple],
    *,
    threshold: float = 0.9,
    num_perm: int = NUM_PERM,
    min_entropy: float = MIN_ENTROPY,
) -> list[list]:
    """Cluster near-duplicate items.

    ``items`` is an iterable of ``(id, text)``. Returns a deterministically
    ordered list of clusters (each a list of >=2 ids) whose members are
    estimated to share Jaccard similarity >= ``threshold``. Items whose text is
    below ``min_entropy`` are gated out (too low-information to dedup reliably).

    LSH banding generates candidate pairs (items sharing any band block); each
    candidate is then verified against the full signature so bands only prune,
    never decide.
    """
    rows = [
        (rid, text) for rid, text in items
        if shannon_entropy(text) >= min_entropy
    ]
    sigs = {rid: minhash_signature(text, num_perm) for rid, text in rows}

    # Exact fast path: identical normalized text is always a duplicate.
    uf = _UnionFind()
    exact: dict[str, list] = {}
    for rid, text in rows:
        exact.setdefault(normalize(text), []).append(rid)
    for group in exact.values():
        for other in group[1:]:
            uf.union(group[0], other)

    # LSH banding -> candidate pairs -> verify with the full signature.
    band_size = max(1, num_perm // LSH_BANDS)
    buckets: dict[tuple, list] = {}
    for rid in sigs:
        sig = sigs[rid]
        for b in range(0, num_perm, band_size):
            key = (b,) + sig[b:b + band_size]
            buckets.setdefault(key, []).append(rid)
    seen_pairs: set = set()
    for members in buckets.values():
        if len(members) < 2:
            continue
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                pair = (members[i], members[j])
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                if _signature_similarity(sigs[pair[0]], sigs[pair[1]]) >= threshold:
                    uf.union(pair[0], pair[1])

    clusters: dict = {}
    for rid in sigs:
        clusters.setdefault(uf.find(rid), []).append(rid)
    result = [sorted(members) for members in clusters.values() if len(members) >= 2]
    result.sort(key=lambda c: (c[0], c))
    return result


# --- Conflict detection (issue #32): topic-similar but body-divergent -------

TOPIC_THRESHOLD = 0.5   # title+keywords similarity: "same subject"
BODY_THRESHOLD = 0.4    # content similarity: at/below this the claims diverge


def find_conflicts(
    items: Iterable[tuple],
    *,
    topic_threshold: float = TOPIC_THRESHOLD,
    body_threshold: float = BODY_THRESHOLD,
    min_entropy: float = MIN_ENTROPY,
) -> list[tuple]:
    """Surface CONFLICT candidates: pairs that are topically similar but whose
    bodies diverge ("same subject, opposite story").

    ``items`` is an iterable of ``(id, topic_text, body_text)``. A pair is a
    candidate iff topic similarity >= ``topic_threshold`` AND body similarity
    <= ``body_threshold``. This is the INVERSE of ``find_near_duplicates`` (high
    similarity on the WHOLE text): a near-duplicate has HIGH body similarity and
    is therefore excluded. Rows whose topic is below ``min_entropy``, or whose
    body has no shingles (can't assess divergence), are skipped.

    Deterministic; returns sorted ``(id_a, id_b)`` pairs with id_a < id_b.
    """
    rows = [
        (rid, topic, body) for rid, topic, body in items
        if shannon_entropy(topic) >= min_entropy and _tokens(body)
    ]
    topic_sig = {rid: minhash_signature(topic) for rid, topic, _ in rows}
    body_sig = {rid: minhash_signature(body) for rid, _, body in rows}
    ids = [rid for rid, _, _ in rows]
    pairs: list[tuple] = []
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            if _signature_similarity(topic_sig[a], topic_sig[b]) < topic_threshold:
                continue
            if _signature_similarity(body_sig[a], body_sig[b]) <= body_threshold:
                pairs.append((a, b) if a <= b else (b, a))
    pairs.sort()
    return pairs
