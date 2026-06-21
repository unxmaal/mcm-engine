"""Backend selection + DSN env-var overrides (Phase 4b deployments).

Containerized deployments don't usually mount a YAML config — the
image ships an empty mcm-engine.yaml (or none) and the operator twists
backend knobs via env vars. The override mechanism MUST work without
any YAML file at all, since App Runner / ECS / k8s typically don't
mount one.
"""
from __future__ import annotations

import os

import pytest

from mcm_engine.config import load_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Strip any MCM_* env vars before each test so they don't leak in
    from the developer's shell."""
    for key in list(os.environ):
        if key.startswith("MCM_"):
            monkeypatch.delenv(key, raising=False)


def test_axis_env_vars_override_yaml(tmp_path, monkeypatch):
    (tmp_path / "mcm-engine.yaml").write_text(
        "project_name: env-test\n"
        "backends:\n"
        "  storage: embedded\n"
        "  counters: embedded\n"
        "  search: embedded\n"
    )
    monkeypatch.setenv("MCM_PROJECT_NAME", "env-test")
    monkeypatch.setenv("MCM_BACKENDS_STORAGE", "postgres")
    monkeypatch.setenv("MCM_BACKENDS_COUNTERS", "redis")
    monkeypatch.setenv("MCM_BACKENDS_SEARCH", "opensearch")

    config = load_config(
        config_path=tmp_path / "mcm-engine.yaml",
        project_root=tmp_path,
    )
    assert config.backends.storage == "postgres"
    assert config.backends.counters == "redis"
    assert config.backends.search == "opensearch"
    assert config.backends.session == "embedded"  # untouched


def test_postgres_dsn_env_populates_options_for_all_postgres_axes(tmp_path, monkeypatch):
    monkeypatch.setenv("MCM_PROJECT_NAME", "x")
    monkeypatch.setenv("MCM_BACKENDS_STORAGE", "postgres")
    monkeypatch.setenv("MCM_BACKENDS_COUNTERS", "postgres")
    monkeypatch.setenv("MCM_BACKENDS_SEARCH", "postgres")
    monkeypatch.setenv("MCM_POSTGRES_DSN", "postgresql://u:p@host/db")

    config = load_config(project_root=tmp_path)
    assert config.backends.storage_options == {"dsn": "postgresql://u:p@host/db"}
    assert config.backends.counters_options == {"dsn": "postgresql://u:p@host/db"}
    assert config.backends.search_options == {"dsn": "postgresql://u:p@host/db"}


def test_redis_url_populates_only_when_counters_is_redis(tmp_path, monkeypatch):
    monkeypatch.setenv("MCM_PROJECT_NAME", "x")
    monkeypatch.setenv("MCM_BACKENDS_COUNTERS", "redis")
    monkeypatch.setenv("MCM_REDIS_URL", "redis://r:6379/0")

    config = load_config(project_root=tmp_path)
    assert config.backends.counters_options == {"url": "redis://r:6379/0"}


def test_opensearch_url_populates_only_when_search_is_opensearch(tmp_path, monkeypatch):
    monkeypatch.setenv("MCM_PROJECT_NAME", "x")
    monkeypatch.setenv("MCM_BACKENDS_SEARCH", "opensearch")
    monkeypatch.setenv("MCM_OPENSEARCH_URL", "https://os.example.com")

    config = load_config(project_root=tmp_path)
    assert config.backends.search_options == {"url": "https://os.example.com"}


def test_yaml_options_take_precedence_over_env_dsn(tmp_path, monkeypatch):
    """If the YAML explicitly sets a DSN, the env-var convenience does
    not silently clobber it (setdefault semantics)."""
    (tmp_path / "mcm-engine.yaml").write_text(
        "project_name: x\n"
        "backends:\n"
        "  storage: postgres\n"
        "  storage_options:\n"
        "    dsn: postgresql://yaml-wins@host/db\n"
    )
    monkeypatch.setenv("MCM_POSTGRES_DSN", "postgresql://env-loses@host/db")

    config = load_config(
        config_path=tmp_path / "mcm-engine.yaml",
        project_root=tmp_path,
    )
    assert config.backends.storage_options == {
        "dsn": "postgresql://yaml-wins@host/db",
    }


def test_no_yaml_and_only_env_works(tmp_path, monkeypatch):
    """Container deployments often ship NO YAML. project_name + the env
    vars alone must be enough."""
    monkeypatch.setenv("MCM_PROJECT_NAME", "container-only")
    monkeypatch.setenv("MCM_BACKENDS_STORAGE", "postgres")
    monkeypatch.setenv("MCM_POSTGRES_DSN", "postgresql://u:p@host/db")

    config = load_config(project_root=tmp_path)
    assert config.project_name == "container-only"
    assert config.backends.storage == "postgres"
    assert config.backends.storage_options == {"dsn": "postgresql://u:p@host/db"}
