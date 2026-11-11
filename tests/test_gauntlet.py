"""The gauntlet ratchets over this repo's registry; written by `gauntlet init`."""

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("gauntlet.ratchet", reason="gauntlet needs Python 3.12")

from gauntlet import ratchet, skill

_spec = importlib.util.spec_from_file_location("gauntlet_registry", Path(__file__).resolve().parent / "gauntlet" / "registry.py")
_registry = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_registry)

globals().update(ratchet.tests(_registry.REGISTRY, skill_dir=skill.corpus_dir()))
