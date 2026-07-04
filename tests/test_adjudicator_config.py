"""Config tests for the Slice 3 adjudicator section (fix_ingestion).

The engine stays model-free by default: with no `adjudicator:` block the section
is present but unconfigured (empty provider). When configured, it follows the
same strict-key hygiene as nudges/backends — an unknown key fails closed.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from mcm_engine.config import AdjudicatorConfig, load_config


def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "mcm-engine.yaml"
    p.write_text(yaml.dump(data), encoding="utf-8")
    return tmp_path


def test_default_config_has_unconfigured_adjudicator(tmp_path):
    root = _write(tmp_path, {"project_name": "x"})
    config = load_config(project_root=root)
    assert isinstance(config.adjudicator, AdjudicatorConfig)
    assert config.adjudicator.provider == ""          # not configured
    assert config.adjudicator.confidence_threshold == 0.7  # default bar


def test_adjudicator_block_is_parsed(tmp_path):
    root = _write(tmp_path, {
        "project_name": "x",
        "adjudicator": {
            "provider": "openai-compatible",
            "base_url": "https://api.example.com/v1",
            "model": "haiku-cheap",
            "api_key_env": "MY_KEY",
            "confidence_threshold": 0.85,
            "review_queue_path": ".claude/queue.jsonl",
        },
    })
    config = load_config(project_root=root)
    adj = config.adjudicator
    assert adj.provider == "openai-compatible"
    assert adj.base_url == "https://api.example.com/v1"
    assert adj.model == "haiku-cheap"
    assert adj.api_key_env == "MY_KEY"
    assert adj.confidence_threshold == 0.85


def test_unknown_adjudicator_key_fails_closed(tmp_path):
    root = _write(tmp_path, {
        "project_name": "x",
        "adjudicator": {"provider": "openai-compatible", "bogus_key": 1},
    })
    with pytest.raises(ValueError, match="adjudicator"):
        load_config(project_root=root)
