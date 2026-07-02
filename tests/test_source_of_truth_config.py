"""Issue #16 — source_of_truth authority axis (config layer).

Replaces the proposed MCM_WATCHER on/off boolean with an explicit,
sealed mode that sets the polarity of every file-coupled behavior:
  files    -> markdown under rules_path is authoritative (World A, default)
  database -> the DB is authoritative; startup file->DB sync is not run (World B)

Precedence and fail-safe:
  - env MCM_SOURCE_OF_TRUTH overrides the YAML value (like every other env override).
  - a malformed value (env OR yaml) falls back to "files" with a warning —
    fail-safe direction is the historical always-files behavior.
"""
from __future__ import annotations

import pytest

from mcm_engine.config import MCMConfig, load_config


def _write_yaml(tmp_path, body: str):
    p = tmp_path / "mcm-engine.yaml"
    p.write_text(body, encoding="utf-8")
    return p


def test_default_is_files(tmp_path, monkeypatch):
    monkeypatch.delenv("MCM_SOURCE_OF_TRUTH", raising=False)
    cfg = load_config(config_path=_write_yaml(tmp_path, "project_name: t\n"),
                      project_root=tmp_path)
    assert cfg.source_of_truth == "files"
    assert cfg.files_are_authoritative is True


def test_yaml_database(tmp_path, monkeypatch):
    monkeypatch.delenv("MCM_SOURCE_OF_TRUTH", raising=False)
    cfg = load_config(
        config_path=_write_yaml(tmp_path, "project_name: t\nsource_of_truth: database\n"),
        project_root=tmp_path)
    assert cfg.source_of_truth == "database"
    assert cfg.files_are_authoritative is False


def test_env_selects_database(tmp_path, monkeypatch):
    monkeypatch.setenv("MCM_SOURCE_OF_TRUTH", "database")
    cfg = load_config(config_path=_write_yaml(tmp_path, "project_name: t\n"),
                      project_root=tmp_path)
    assert cfg.source_of_truth == "database"


def test_env_overrides_yaml(tmp_path, monkeypatch):
    monkeypatch.setenv("MCM_SOURCE_OF_TRUTH", "database")
    cfg = load_config(
        config_path=_write_yaml(tmp_path, "project_name: t\nsource_of_truth: files\n"),
        project_root=tmp_path)
    assert cfg.source_of_truth == "database"


def test_malformed_env_falls_back_to_files(tmp_path, monkeypatch):
    monkeypatch.setenv("MCM_SOURCE_OF_TRUTH", "weird")
    cfg = load_config(config_path=_write_yaml(tmp_path, "project_name: t\n"),
                      project_root=tmp_path)
    assert cfg.source_of_truth == "files"
    assert cfg.files_are_authoritative is True


def test_malformed_yaml_falls_back_to_files(tmp_path, monkeypatch):
    monkeypatch.delenv("MCM_SOURCE_OF_TRUTH", raising=False)
    cfg = load_config(
        config_path=_write_yaml(tmp_path, "project_name: t\nsource_of_truth: nonsense\n"),
        project_root=tmp_path)
    assert cfg.source_of_truth == "files"


def test_direct_construction_defaults_and_helper():
    cfg = MCMConfig(project_name="t")
    assert cfg.source_of_truth == "files"
    assert cfg.files_are_authoritative is True
    assert MCMConfig(project_name="t", source_of_truth="database").files_are_authoritative is False
