"""MetricsConfig parsing (Phase 3)."""
from __future__ import annotations

import os

import pytest

from mcm_engine.config import MetricsConfig, load_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("MCM_"):
            monkeypatch.delenv(key, raising=False)


def test_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("MCM_PROJECT_NAME", "x")
    config = load_config(project_root=tmp_path)  # no yaml
    assert isinstance(config.metrics, MetricsConfig)
    assert config.metrics.enabled is True
    assert config.metrics.report_limit == 3


def test_yaml_metrics_block(tmp_path):
    (tmp_path / "mcm-engine.yaml").write_text(
        "project_name: x\n"
        "metrics:\n"
        "  enabled: false\n"
        "  report_limit: 10\n"
    )
    config = load_config(config_path=tmp_path / "mcm-engine.yaml", project_root=tmp_path)
    assert config.metrics.enabled is False
    assert config.metrics.report_limit == 10


def test_unknown_metrics_key_fails_closed(tmp_path):
    (tmp_path / "mcm-engine.yaml").write_text(
        "project_name: x\n"
        "metrics:\n"
        "  bogus: 1\n"
    )
    with pytest.raises(ValueError, match="unknown metrics key"):
        load_config(config_path=tmp_path / "mcm-engine.yaml", project_root=tmp_path)


def test_env_toggle_disables(tmp_path, monkeypatch):
    monkeypatch.setenv("MCM_PROJECT_NAME", "x")
    monkeypatch.setenv("MCM_METRICS_ENABLED", "0")
    config = load_config(project_root=tmp_path)
    assert config.metrics.enabled is False


def test_yaml_wins_over_env(tmp_path, monkeypatch):
    (tmp_path / "mcm-engine.yaml").write_text(
        "project_name: x\n"
        "metrics:\n"
        "  enabled: true\n"
    )
    monkeypatch.setenv("MCM_METRICS_ENABLED", "0")
    config = load_config(config_path=tmp_path / "mcm-engine.yaml", project_root=tmp_path)
    assert config.metrics.enabled is True
