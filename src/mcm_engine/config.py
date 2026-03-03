"""MCM Engine configuration — dataclass loaded from YAML + env vars."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Union

import yaml


@dataclass
class NudgeConfig:
    """Thresholds for behavioral nudges."""

    store_reminder_turns: int = 10
    checkpoint_turns: int = 25
    mandatory_stop_turns: int = 50
    hyper_focus_threshold: int = 3
    rules_check_interval: int = 15


@dataclass
class MCMConfig:
    """Top-level configuration for an MCM Engine instance."""

    project_name: str
    db_path: str = ".claude/knowledge.db"
    log_path: str = ""
    plugins: list[str] = field(default_factory=list)
    nudges: NudgeConfig = field(default_factory=NudgeConfig)
    rules_path: Union[str, list[str]] = "rules/"
    server_name: str = ""
    server_instructions: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not self.server_name:
            self.server_name = f"{self.project_name}-knowledge"
        if not self.log_path:
            self.log_path = f"/tmp/{self.project_name}-mcm.log"

    def resolve_db_path(self, project_root: Path) -> Path:
        """Resolve db_path relative to project root."""
        p = Path(self.db_path)
        if p.is_absolute():
            return p
        return project_root / p

    def resolve_rules_paths(self, project_root: Path) -> list[Path]:
        """Resolve rules_path(s) relative to project root.

        Accepts a single string or a list of strings. Returns a list of
        resolved Path objects. The first path is the "primary" rules
        directory where new rule files are created.
        """
        raw = self.rules_path if isinstance(self.rules_path, list) else [self.rules_path]
        result: list[Path] = []
        for entry in raw:
            p = Path(entry)
            result.append(p if p.is_absolute() else project_root / p)
        return result


def load_config(config_path: Path | None = None, project_root: Path | None = None) -> MCMConfig:
    """Load config from YAML file + env var overrides.

    Search order for config file:
    1. Explicit config_path argument
    2. MCM_CONFIG env var
    3. mcm-engine.yaml in project_root
    4. mcm-engine.yaml in cwd
    """
    if project_root is None:
        project_root = Path.cwd()

    # Find config file
    if config_path is None:
        env_path = os.environ.get("MCM_CONFIG")
        if env_path:
            config_path = Path(env_path)
        else:
            candidate = project_root / "mcm-engine.yaml"
            if candidate.exists():
                config_path = candidate
            else:
                candidate = Path.cwd() / "mcm-engine.yaml"
                if candidate.exists():
                    config_path = candidate

    # Load YAML
    raw: dict[str, Any] = {}
    if config_path and config_path.exists():
        with open(config_path) as f:
            raw = yaml.safe_load(f) or {}

    # Env var overrides (MCM_PROJECT_NAME, MCM_DB_PATH, MCM_LOG_PATH, MCM_SERVER_NAME)
    env_map = {
        "MCM_PROJECT_NAME": "project_name",
        "MCM_DB_PATH": "db_path",
        "MCM_LOG_PATH": "log_path",
        "MCM_SERVER_NAME": "server_name",
        "MCM_SERVER_INSTRUCTIONS": "server_instructions",
        "MCM_RULES_PATH": "rules_path",
    }
    for env_key, config_key in env_map.items():
        val = os.environ.get(env_key)
        if val is not None:
            if config_key == "rules_path" and ":" in val:
                raw[config_key] = val.split(":")
            else:
                raw[config_key] = val

    # Validate required fields
    if "project_name" not in raw:
        raise ValueError(
            "project_name is required. Set it in mcm-engine.yaml or MCM_PROJECT_NAME env var."
        )

    # Extract nudges sub-config
    nudge_raw = raw.pop("nudges", {})
    nudges = NudgeConfig(**{k: v for k, v in nudge_raw.items() if k in NudgeConfig.__dataclass_fields__})

    # Build config
    known_fields = set(MCMConfig.__dataclass_fields__.keys()) - {"nudges"}
    config_kwargs = {k: v for k, v in raw.items() if k in known_fields}
    config_kwargs["nudges"] = nudges

    # Everything else goes into extra
    extra = {k: v for k, v in raw.items() if k not in known_fields}
    if extra:
        config_kwargs.setdefault("extra", {}).update(extra)

    return MCMConfig(**config_kwargs)
