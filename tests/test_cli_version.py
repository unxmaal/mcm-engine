"""Bug: mcm-engine had no --version/-v flag and -h didn't show the
installed version, making troubleshooting harder. The fix also exposes
__version__ on the package itself, sourced from importlib.metadata so
it can never drift from pyproject.toml again (it had drifted to
0.1.0 while pyproject was at 0.4.0)."""
from __future__ import annotations

import sys
from importlib.metadata import version

import pytest

import mcm_engine
from mcm_engine.cli import main


def _expected_version() -> str:
    return version("mcm-engine")


def test_dunder_version_matches_installed_dist():
    """__version__ on the package must match the installed dist version,
    not a hardcoded string that can drift from pyproject.toml."""
    assert mcm_engine.__version__ == _expected_version()


def test_long_flag_prints_version_and_exits_zero(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mcm-engine", "--version"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert _expected_version() in captured.out


def test_short_flag_prints_version_and_exits_zero(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["mcm-engine", "-v"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert _expected_version() in captured.out


def test_help_includes_version(capsys, monkeypatch):
    """`-h` output should mention the version so users can troubleshoot
    without remembering a separate flag."""
    monkeypatch.setattr(sys, "argv", ["mcm-engine", "-h"])
    with pytest.raises(SystemExit) as exc_info:
        main()
    assert exc_info.value.code == 0
    captured = capsys.readouterr()
    assert _expected_version() in captured.out
