"""Env-overridable guessed constants (#149): the helper parses correctly and
each wired module honors its env var, while defaults stay byte-identical when no
env var is set.
"""
from __future__ import annotations

import importlib

import pytest

from mcm_engine import tunables


# ---- helper ----

def test_env_float_unset_uses_default(monkeypatch):
    monkeypatch.delenv("MCM_X", raising=False)
    assert tunables.env_float("MCM_X", 1.5) == 1.5


def test_env_float_parses_and_falls_back(monkeypatch):
    monkeypatch.setenv("MCM_X", "2.75")
    assert tunables.env_float("MCM_X", 1.5) == 2.75
    monkeypatch.setenv("MCM_X", "not a number")
    assert tunables.env_float("MCM_X", 1.5) == 1.5
    monkeypatch.setenv("MCM_X", "   ")
    assert tunables.env_float("MCM_X", 1.5) == 1.5


def test_env_int_parses_and_falls_back(monkeypatch):
    monkeypatch.setenv("MCM_N", "7")
    assert tunables.env_int("MCM_N", 3) == 7
    monkeypatch.setenv("MCM_N", "3.5")  # not an int
    assert tunables.env_int("MCM_N", 3) == 3


# ---- module wiring: override applies on (re)import, default otherwise ----

def _reload(modname):
    mod = importlib.import_module(modname)
    return importlib.reload(mod)


@pytest.mark.parametrize("envvar,modname,attr,default,override", [
    ("MCM_SCORE_W_RELEVANCE", "mcm_engine.scoring", "W_RELEVANCE", 2.0, 3.0),
    ("MCM_DEDUP_TOPIC_THRESHOLD", "mcm_engine.dedup", "TOPIC_THRESHOLD", 0.5, 0.7),
    ("MCM_SEARCH_RANK_THRESHOLD", "mcm_engine.tools.search", "RANK_THRESHOLD", 1.0, 0.25),
    ("MCM_SEARCH_STALE_DAYS", "mcm_engine.tools.search", "STALE_DAYS", 90, 30),
    ("MCM_RULESIFT_KNOWN_THRESHOLD", "mcm_engine.ingest.rulesift", "KNOWN_THRESHOLD", 0.9, 0.95),
])
def test_constant_defaults_and_overrides(monkeypatch, envvar, modname, attr, default, override):
    # Default when unset.
    monkeypatch.delenv(envvar, raising=False)
    mod = _reload(modname)
    assert getattr(mod, attr) == default
    # Override when set.
    monkeypatch.setenv(envvar, str(override))
    mod = _reload(modname)
    assert getattr(mod, attr) == override
    # Restore the module to its default state for other tests.
    monkeypatch.delenv(envvar, raising=False)
    _reload(modname)


def test_structural_dedup_params_are_not_env_wired():
    """NUM_PERM/LSH_BANDS/SHINGLE_N are algorithm structure, not tunable knobs:
    env-overriding them would break the MinHash signature contract. They stay
    literal while MIN_ENTROPY (a behavior gate) is env-wired."""
    import inspect

    from mcm_engine import dedup
    src = inspect.getsource(dedup)
    assert "NUM_PERM = 32" in src
    assert "LSH_BANDS = 8" in src
    assert "SHINGLE_N = 2" in src
    assert 'env_float("MCM_DEDUP_MIN_ENTROPY"' in src
