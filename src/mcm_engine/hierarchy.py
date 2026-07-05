"""Rule hierarchy vocabulary (issue #64, Phase 1).

Three orthogonal axes on rules, distinct from the existing confidence/lifecycle
axis (`status`, `correct_count`, `superseded_by`, ...):

- ``importance`` — ordinal blast-radius rank. Higher binds harder. A 3-tier
  ladder: reference (0) < default (1) < invariant (2).
- ``scope`` — ``universal`` (always live) vs ``conditional`` (situational).
- ``kind`` — ``directive`` (a rule you can enforce) vs ``fact`` (recall-only).

These answer different questions and are deliberately kept separate from the
topical ``category`` string. Defaults are the most conservative: a freshly
added rule is a low-importance, situational fact until it is deliberately
promoted. Promotion into the top tier is an act, not a side effect of
reinforcement (reinforcement tracks popularity/scope, not blast-radius).

This module is the single source of the vocab so the storage layer, the MCP
verbs, and the admin tuning UI (later phases) all agree on the allowed values.
"""
from __future__ import annotations

SCOPES: tuple[str, ...] = ("universal", "conditional")
KINDS: tuple[str, ...] = ("directive", "fact")

# Importance tiers (ordinal; higher = binds harder).
IMPORTANCE_REFERENCE = 0   # situational fact / default; recall-only
IMPORTANCE_DEFAULT = 1     # strong preference; surfaced proactively
IMPORTANCE_INVARIANT = 2   # always-in-context; hard-enforced where mechanical

IMPORTANCE_MIN = IMPORTANCE_REFERENCE
IMPORTANCE_MAX = IMPORTANCE_INVARIANT

DEFAULT_IMPORTANCE = IMPORTANCE_REFERENCE
DEFAULT_SCOPE = "conditional"
DEFAULT_KIND = "fact"


def valid_scope(scope: object) -> bool:
    return scope in SCOPES


def valid_kind(kind: object) -> bool:
    return kind in KINDS


def valid_importance(importance: object) -> bool:
    # bool is an int subclass; exclude it so True/False don't sneak through.
    return (
        isinstance(importance, int)
        and not isinstance(importance, bool)
        and IMPORTANCE_MIN <= importance <= IMPORTANCE_MAX
    )


def normalize_importance(importance: int) -> int:
    """Clamp an importance to the valid ordinal range."""
    return max(IMPORTANCE_MIN, min(IMPORTANCE_MAX, int(importance)))


def validated_metadata_updates(
    importance: object = None,
    scope: object = None,
    kind: object = None,
    category: object = None,
) -> dict:
    """Build the dict of hierarchy fields to write, validating each provided
    value against the vocab. ``None`` means "leave unchanged" and is omitted.
    Raises ``ValueError`` on any invalid value so the write is rejected before
    it touches storage. ``category`` is free-form (a topical namespace, no
    controlled vocabulary). Shared by both storage adapters so the sqlite and
    postgres write paths validate identically.
    """
    updates: dict = {}
    if importance is not None:
        if not valid_importance(importance):
            raise ValueError(
                f"invalid importance {importance!r}; expected int "
                f"{IMPORTANCE_MIN}..{IMPORTANCE_MAX}"
            )
        updates["importance"] = importance
    if scope is not None:
        if not valid_scope(scope):
            raise ValueError(f"invalid scope {scope!r}; expected one of {SCOPES}")
        updates["scope"] = scope
    if kind is not None:
        if not valid_kind(kind):
            raise ValueError(f"invalid kind {kind!r}; expected one of {KINDS}")
        updates["kind"] = kind
    if category is not None:
        updates["category"] = category
    return updates
