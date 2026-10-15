"""Env overrides for guessed behavior-shaping constants (issue #149).

Several numeric constants that shape ranking, dedup, search and ingest were
chosen by hand with no backing evidence. They are not wrong, but they are
unvalidated, and the KB records repeatedly that an invented threshold misleads
until it is measured (FINDING #163, RULE #303, FINDING #5). A real retune needs
an evaluation harness (#137); this module is the prerequisite: it lets each knob
be overridden from the environment so it can be swept WITHOUT a rebuild.

Defaults are unchanged — a knob reads its env var only when one is set, so
behavior is byte-identical unless an operator opts in. A malformed value falls
back to the default rather than crashing. Read these at import, the same way the
hook reads ``MCM_FIXATION_THRESHOLD``.

Naming: ``MCM_<AREA>_<CONSTANT>`` (e.g. ``MCM_SCORE_W_RELEVANCE``). The operator
owns the values once they override; invariants the defaults satisfy (e.g.
scoring's W_RELEVANCE > the summed other non-pinned weights) are not re-checked.
"""
from __future__ import annotations

import os


def env_float(name: str, default: float) -> float:
    """``float`` from env var ``name``, else ``default``. Malformed -> default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def env_int(name: str, default: int) -> int:
    """``int`` from env var ``name``, else ``default``. Malformed -> default."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default
