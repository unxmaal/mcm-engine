"""MCM2-06: config hygiene.

Unknown keys must fail loudly, not silently drop. This was the audited
bug at config.py:130 (nudge keys silently filtered against
NudgeConfig.__dataclass_fields__) and the analogous behavior in load_config
for top-level keys (which silently accumulate into MCMConfig.extra).
"""
from __future__ import annotations

import textwrap

import pytest

from mcm_engine.config import load_config


def _write_yaml(tmp_path, body: str):
    path = tmp_path / "mcm-engine.yaml"
    path.write_text(textwrap.dedent(body))
    return path


# ---- nudge subkey hygiene ------------------------------------------------


def test_known_nudge_keys_are_accepted(tmp_path):
    """Sanity: known nudge keys do not raise."""
    cfg_path = _write_yaml(tmp_path, """
        project_name: test
        nudges:
          store_reminder_turns: 7
          checkpoint_turns: 20
    """)
    cfg = load_config(config_path=cfg_path, project_root=tmp_path)
    assert cfg.nudges.store_reminder_turns == 7
    assert cfg.nudges.checkpoint_turns == 20


def test_unknown_nudge_key_raises(tmp_path):
    """Unknown nudge subkeys must raise — no silent drop.

    The bug: today config.py:130 filters nudge_raw against
    NudgeConfig.__dataclass_fields__, silently dropping anything not in
    the field list. A typo like `store_reminders_turns` (extra 's') is
    invisibly ignored; the user wonders why the threshold didn't change.
    """
    cfg_path = _write_yaml(tmp_path, """
        project_name: test
        nudges:
          store_reminders_turns: 7   # typo — extra s
    """)
    with pytest.raises(ValueError, match=r"unknown nudge key.*store_reminders_turns"):
        load_config(config_path=cfg_path, project_root=tmp_path)


def test_unknown_nudge_key_error_lists_valid_keys(tmp_path):
    """Error message names the typo and lists valid keys, not just 'invalid'.

    Future-Eric reading the error needs to see what the right name is.
    """
    cfg_path = _write_yaml(tmp_path, """
        project_name: test
        nudges:
          mandatory_stops_blocking: true
    """)
    with pytest.raises(ValueError) as exc:
        load_config(config_path=cfg_path, project_root=tmp_path)
    msg = str(exc.value)
    assert "mandatory_stops_blocking" in msg
    # And at least one real valid key, so the user can correct.
    assert "store_reminder_turns" in msg or "mandatory_stop_blocking" in msg


# ---- top-level key hygiene ----------------------------------------------


def test_known_top_level_keys_are_accepted(tmp_path):
    """Sanity: all known top-level keys work."""
    cfg_path = _write_yaml(tmp_path, """
        project_name: test
        db_path: .cache/k.db
        log_path: /tmp/k.log
        plugins: []
        rules_path: rules/
        server_name: test-srv
        server_instructions: hello
    """)
    cfg = load_config(config_path=cfg_path, project_root=tmp_path)
    assert cfg.project_name == "test"
    assert cfg.db_path == ".cache/k.db"


def test_explicit_extra_block_is_accepted(tmp_path):
    """An explicit `extra:` block is the documented escape hatch for
    plugin-specific or future-compat config. It must keep working."""
    cfg_path = _write_yaml(tmp_path, """
        project_name: test
        extra:
          plugin_x_setting: 42
          plugin_y_setting: hello
    """)
    cfg = load_config(config_path=cfg_path, project_root=tmp_path)
    assert cfg.extra == {"plugin_x_setting": 42, "plugin_y_setting": "hello"}


def test_unknown_top_level_key_raises(tmp_path):
    """Unknown top-level keys must raise — no silent accumulation into
    MCMConfig.extra.

    The bug class: today's load_config does
        extra = {k:v for k,v in raw.items() if k not in known_fields}
        if extra: config_kwargs.setdefault("extra", {}).update(extra)
    which means a top-level typo like `dbpath:` (missing underscore)
    silently merges into .extra and the engine boots with the default
    db_path. Catch this at load time.
    """
    cfg_path = _write_yaml(tmp_path, """
        project_name: test
        dbpath: .cache/wrong.db   # typo — should be db_path
    """)
    with pytest.raises(ValueError, match=r"unknown.*dbpath"):
        load_config(config_path=cfg_path, project_root=tmp_path)


def test_unknown_top_level_error_lists_valid_keys(tmp_path):
    """Error message guides the user to the correct key."""
    cfg_path = _write_yaml(tmp_path, """
        project_name: test
        rulespath: rules/    # typo
    """)
    with pytest.raises(ValueError) as exc:
        load_config(config_path=cfg_path, project_root=tmp_path)
    msg = str(exc.value)
    assert "rulespath" in msg
    assert "rules_path" in msg or "project_name" in msg
