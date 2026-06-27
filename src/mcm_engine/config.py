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
    # When True, tools are blocked (return error) after mandatory_stop_turns +
    # mandatory_stop_grace calls without a session_handoff/save_snapshot.
    # Set to False to revert to advisory-only nudges.
    mandatory_stop_blocking: bool = False
    mandatory_stop_grace: int = 5
    # Nudge escalation: after this many ignored nudges of the same type,
    # escalate to MandatoryStopError blocking.
    nudge_escalation_threshold: int = 3
    # Per-tool deficit counters: {tool_name: max_calls_without_it}. When a
    # tracked tool hasn't fired in N tool calls, a targeted nudge names that
    # SPECIFIC tool — unlike store_reminder, which any store tool clears. These
    # escalate to a block through nudge_escalation_threshold like any nudge.
    # On by default for the tools that the aggregate counters never surface.
    periodic_tools: dict[str, int] = field(default_factory=lambda: {
        "link_knowledge": 25,
        "add_negative": 40,
    })


@dataclass
class BackendsConfig:
    """Selects which adapter implements each external concern.

    Names resolve via the AdapterRegistry: bare names hit the entry-point
    + manual-registration tables; "module:Class" syntax imports directly.
    All four default to "embedded" — the in-process reference adapter
    shipped with the engine.
    """

    storage: str = "embedded"
    counters: str = "embedded"
    search: str = "embedded"
    session: str = "embedded"

    # Per-adapter kwargs passed to __init__ (e.g., DSN for Postgres,
    # URL for Redis). Each is a free-form dict; the adapter validates.
    storage_options: dict[str, Any] = field(default_factory=dict)
    counters_options: dict[str, Any] = field(default_factory=dict)
    search_options: dict[str, Any] = field(default_factory=dict)
    session_options: dict[str, Any] = field(default_factory=dict)


@dataclass
class MCMConfig:
    """Top-level configuration for an MCM Engine instance."""

    project_name: str
    db_path: str = ".claude/knowledge.db"
    log_path: str = ""
    plugins: list[str] = field(default_factory=list)
    nudges: NudgeConfig = field(default_factory=NudgeConfig)
    backends: BackendsConfig = field(default_factory=BackendsConfig)
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

    # Extract nudges sub-config — fail closed on unknown keys (MCM2-06).
    nudge_raw = raw.pop("nudges", {})
    nudge_fields = NudgeConfig.__dataclass_fields__
    unknown_nudges = sorted(set(nudge_raw) - set(nudge_fields))
    if unknown_nudges:
        valid = ", ".join(sorted(nudge_fields))
        raise ValueError(
            f"unknown nudge key(s): {', '.join(unknown_nudges)}. "
            f"Valid nudge keys: {valid}"
        )
    nudges = NudgeConfig(**nudge_raw)

    # Extract backends sub-config — same strict-key hygiene (MCM2-04, MCM2-06).
    backends_raw = raw.pop("backends", {})
    backends_fields = BackendsConfig.__dataclass_fields__
    unknown_backends = sorted(set(backends_raw) - set(backends_fields))
    if unknown_backends:
        valid = ", ".join(sorted(backends_fields))
        raise ValueError(
            f"unknown backends key(s): {', '.join(unknown_backends)}. "
            f"Valid backends keys: {valid}"
        )

    # Env-var overrides for backend selection (Phase 4b deployments
    # often have the YAML baked into the image and twist knobs only
    # through env vars).
    backend_axes = {
        "MCM_BACKENDS_STORAGE":  "storage",
        "MCM_BACKENDS_COUNTERS": "counters",
        "MCM_BACKENDS_SEARCH":   "search",
        "MCM_BACKENDS_SESSION":  "session",
    }
    for env_key, axis in backend_axes.items():
        val = os.environ.get(env_key)
        if val is not None:
            backends_raw[axis] = val

    # Convenience env vars that populate the most common adapter options
    # (DSNs / URLs) without forcing operators to write a YAML block.
    # These map straight into the *_options dicts below.
    pg_dsn = os.environ.get("MCM_POSTGRES_DSN")
    redis_url = os.environ.get("MCM_REDIS_URL")
    opensearch_url = os.environ.get("MCM_OPENSEARCH_URL")

    backends = BackendsConfig(**backends_raw)
    if pg_dsn:
        if backends.storage == "postgres":
            backends.storage_options.setdefault("dsn", pg_dsn)
        if backends.counters == "postgres":
            backends.counters_options.setdefault("dsn", pg_dsn)
        if backends.search == "postgres":
            backends.search_options.setdefault("dsn", pg_dsn)
    if redis_url and backends.counters == "redis":
        backends.counters_options.setdefault("url", redis_url)
    if opensearch_url and backends.search == "opensearch":
        backends.search_options.setdefault("url", opensearch_url)

    # Build top-level config — fail closed on unknown keys, except for the
    # explicit `extra:` block which is the documented escape hatch.
    known_fields = set(MCMConfig.__dataclass_fields__.keys()) - {"nudges", "backends"}
    unknown_top = sorted(set(raw) - known_fields)
    if unknown_top:
        valid = ", ".join(sorted(known_fields))
        raise ValueError(
            f"unknown top-level config key(s): {', '.join(unknown_top)}. "
            f"Valid keys: {valid}. "
            f"For plugin-specific or future-compat settings, nest them under `extra:`."
        )
    config_kwargs = {k: v for k, v in raw.items() if k in known_fields}
    config_kwargs["nudges"] = nudges
    config_kwargs["backends"] = backends

    return MCMConfig(**config_kwargs)
