"""Graded actor->weight trust map for outcome correctness (issue #36).

Complements the author!=judge guard (#24): an independent actor's outcome report
can be weighted by how much that actor is trusted. Weights come from the
``MCM_TRUST_WEIGHTS`` env var (a JSON object, actor -> float), with
``MCM_TRUST_DEFAULT`` (default 1.0) for actors not listed. Empty/unset -> every
actor weighs 1.0, so correctness ranking is byte-identical to before.

Applied LATE-BINDING: correctness is recomputed from the ``rule_outcomes`` ledger
at rank time (see ``tools/search.py``), so retuning the map reweights history
with no migration and nothing persisted. Read live from env (cached per
env-value) so a retune takes effect without code changes.

NOTE: v1 is env-driven. A ``trust_weights`` yaml field on MCMConfig is a small
follow-up (it would export into this env var at load time).
"""
from __future__ import annotations

import json
import os

_ENV = "MCM_TRUST_WEIGHTS"
_DEFAULT_ENV = "MCM_TRUST_DEFAULT"
_cache: dict = {}


def _load() -> tuple[dict, float]:
    raw = os.environ.get(_ENV, "")
    default_raw = os.environ.get(_DEFAULT_ENV, "1.0")
    key = (raw, default_raw)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    weights: dict = {}
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                weights = {str(k): float(v) for k, v in parsed.items()}
        except (ValueError, TypeError):
            weights = {}
    try:
        default = float(default_raw)
    except ValueError:
        default = 1.0
    _cache[key] = (weights, default)
    return _cache[key]


def actor_weight(actor: str) -> float:
    """Trust weight for an actor's outcome report (default 1.0 / MCM_TRUST_DEFAULT
    for actors not in the map)."""
    weights, default = _load()
    return weights.get(actor or "nobody", default)
