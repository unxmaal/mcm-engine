"""Issue #208: the PreToolUse hook denies a table of known-bad Bash commands."""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from mcm_engine.hooks import mcp_enforcement
from mcm_engine.hooks.deny_table import DENY_TABLE, Context, find_denial

CTX = Context(cwd=Path("."))

POSITIVE = {
    "pgrep-wait-loop": [
        "while pgrep -f 'pytest tests' >/dev/null; do sleep 5; done",
        "until ! pgrep -f myjob; do sleep 1; done; echo done",
        'while [ -n "$(pgrep -f worker)" ]; do sleep 2; done',
        "for i in $(seq 60); do pgrep -fl build || break; sleep 5; done",
        "timeout 600 bash -c 'while pgrep -f x; do sleep 1; done'",
    ],
    "git-add-all": [
        "git add -A",
        "git add .",
        "git add --all && git commit -m wip",
        "cd repo && /usr/bin/git -C sub add -A",
        "rtk git add -Av",
    ],
    "piped-test-then-commit": [
        "pytest tests/ | tail -5 && git commit -m x",
        "uv run python -m pytest -q 2>&1 | tail -20 && git add a.py && git commit -m x",
        "make check | tee log && git commit -am x",
        "make test 2>&1 | grep -v warn | tail -3 && git commit -m Green",
    ],
    "launchctl-localharness": [
        "launchctl disable gui/501/com.unxmaal.localharness.runner",
        "launchctl unload ~/Library/LaunchAgents/com.unxmaal.localharness.web.plist",
        "sudo launchctl bootout gui/501/com.unxmaal.localharness.queue",
    ],
    "pytest-n-auto": [
        "pytest -n auto tests/",
        "uv run python -m pytest -nauto",
        "pytest --numprocesses=auto",
        'make check PYTEST_ARGS="-n auto"',
    ],
    "gh-pr-merge": [
        "gh pr merge 12 --squash",
        "gh pr merge --admin 12",
    ],
}

NEGATIVE = {
    "pgrep-wait-loop": [
        "pgrep -f x",
        "pgrep -f x; for f in *.py; do echo $f; done",
        "while kill -0 $pid; do sleep 5; done",
        'git commit -m "replace while pgrep -f loops"',
        "cat <<EOF > notes\nwhile pgrep -f x; do sleep 1; done\nEOF",
    ],
    "git-add-all": [
        "git add path/-A-file",
        "git add src/foo.py tests/test_foo.py",
        "git add ./src/foo.py",
        'gh issue create --body "never git add -A"',
        "echo 'git add .'",
    ],
    "piped-test-then-commit": [
        "pytest | tail",
        "pytest -q && git commit -m x",
        "set -o pipefail; pytest | tail -5 && git commit -m x",
        "git log | head && git commit -m x",
        "pytest | tail; echo ok",
    ],
    "launchctl-localharness": [
        "launchctl print gui/501/com.unxmaal.localharness.runner",
        "launchctl unload ~/Library/LaunchAgents/com.other.agent.plist",
        "launchctl list | grep localharness",
    ],
    "pytest-n-auto": [
        "pytest -n 6",
        "pytest -k auto",
        "make check",
        "grep -n auto pytest.ini",
    ],
    "gh-pr-merge": [
        "gh pr merge 12 --auto --squash",
        "gh pr merge 12 --disable-auto",
        "gh pr view 12",
        'gh pr comment 12 --body "do not gh pr merge"',
    ],
}

TABLE_IDS = {p.id for p in DENY_TABLE}


def test_every_pattern_has_fixtures():
    with_pos = set(POSITIVE) | {"discard-unowned-changes"}
    with_neg = set(NEGATIVE) | {"discard-unowned-changes"}
    assert with_pos == TABLE_IDS and with_neg == TABLE_IDS


def test_every_pattern_names_reason_replacement_and_kb():
    for p in DENY_TABLE:
        assert p.reason and p.replacement and p.kb


@pytest.mark.parametrize("pid,cmd", [(k, c) for k, v in POSITIVE.items() for c in v])
def test_positive_is_denied_by_its_pattern(pid, cmd):
    hit = find_denial(cmd, CTX)
    assert hit is not None and hit.id == pid


@pytest.mark.parametrize("pid,cmd", [(k, c) for k, v in POSITIVE.items() for c in v])
def test_red_proof_removing_pattern_lets_positive_through(pid, cmd):
    table = [p for p in DENY_TABLE if p.id != pid]
    hit = find_denial(cmd, CTX, table)
    assert hit is None or hit.id != pid


@pytest.mark.parametrize("pid,cmd", [(k, c) for k, v in NEGATIVE.items() for c in v])
def test_negative_near_miss_is_allowed(pid, cmd):
    assert find_denial(cmd, CTX) is None


# --- discard-unowned-changes needs a real repo ------------------------------


@pytest.fixture
def repo(tmp_path):
    def git(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "t@t")
    git("config", "user.name", "t")
    for name in ("a.py", "b.py"):
        (tmp_path / name).write_text("x = 1\n")
    git("add", "a.py", "b.py")
    git("commit", "-qm", "init")
    (tmp_path / "a.py").write_text("x = 2\n")
    (tmp_path / "new.py").write_text("y\n")
    return tmp_path


@pytest.mark.parametrize("cmd", [
    "git checkout -- a.py",
    "git restore a.py",
    "git checkout .",
    "git restore --worktree --staged a.py",
])
def test_discard_of_unowned_change_is_denied(repo, cmd):
    hit = find_denial(cmd, Context(cwd=repo))
    assert hit is not None and hit.id == "discard-unowned-changes"


def test_discard_follows_cd(repo, tmp_path_factory):
    elsewhere = tmp_path_factory.mktemp("elsewhere")
    hit = find_denial(f"cd {repo} && git checkout -- a.py", Context(cwd=elsewhere))
    assert hit is not None and hit.id == "discard-unowned-changes"


def test_red_proof_discard(repo):
    table = [p for p in DENY_TABLE if p.id != "discard-unowned-changes"]
    assert find_denial("git checkout -- a.py", Context(cwd=repo), table) is None


@pytest.mark.parametrize("cmd", [
    "git checkout -- b.py",         # clean file
    "git restore new.py",           # untracked: restore cannot touch it
    "git restore --staged a.py",    # unstage only, worktree kept
    "git checkout main",            # branch switch, not a path discard
])
def test_discard_near_miss_is_allowed(repo, cmd):
    assert find_denial(cmd, Context(cwd=repo)) is None


def test_discard_of_change_this_agent_made_is_allowed(repo):
    ctx = Context(cwd=repo, edited_files=frozenset({str(repo / "a.py")}))
    assert find_denial("git checkout -- a.py", ctx) is None


def test_discard_outside_a_repo_fails_open(tmp_path):
    assert find_denial("git checkout -- a.py", Context(cwd=tmp_path)) is None


# --- main(): the deny reaches Claude Code and is logged ---------------------


def _run_main(event):
    out, err = io.StringIO(), io.StringIO()
    with patch.object(sys, "stdin", io.StringIO(json.dumps(event))), \
            patch.object(sys, "stdout", out), patch.object(sys, "stderr", err):
        rc = mcp_enforcement.main()
    return rc, out.getvalue(), err.getvalue()


def _bash(tmp_path, cmd):
    (tmp_path / ".git").mkdir(exist_ok=True)
    return {"tool_name": "Bash", "session_id": "s1", "cwd": str(tmp_path),
            "tool_input": {"command": cmd}}


def test_main_emits_deny_with_reason_and_replacement(tmp_path, monkeypatch):
    monkeypatch.setenv("MCM_COVERAGE_GATE", "0")
    rc, out, _ = _run_main(_bash(tmp_path, "git add -A"))
    assert rc == 0
    hso = json.loads(out)["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "untracked" in hso["permissionDecisionReason"]
    assert "stage the named files" in hso["permissionDecisionReason"]
    events = [json.loads(l) for l in (tmp_path / ".claude" / "mcp-enforcement-events.jsonl")
              .read_text().splitlines()]
    assert events[-1]["action"] == "deny" and events[-1]["pattern"] == "git-add-all"


def test_main_allows_innocent_bash_silently(tmp_path, monkeypatch):
    monkeypatch.setenv("MCM_COVERAGE_GATE", "0")
    rc, out, _ = _run_main(_bash(tmp_path, "git add src/foo.py"))
    assert rc == 0 and out == ""


def test_main_fails_open_when_the_check_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("MCM_COVERAGE_GATE", "0")

    def boom(*a, **k):
        raise RuntimeError("bug in the table")
    monkeypatch.setattr(mcp_enforcement, "find_denial", boom)
    rc, out, _ = _run_main(_bash(tmp_path, "git add -A"))
    assert rc == 0 and "deny" not in out


def test_deny_applies_only_to_bash(tmp_path, monkeypatch):
    monkeypatch.setenv("MCM_COVERAGE_GATE", "0")
    event = _bash(tmp_path, "git add -A")
    event["tool_name"] = "Write"
    event["tool_input"] = {"file_path": str(tmp_path / "x.sh"), "content": "git add -A"}
    rc, out, _ = _run_main(event)
    assert rc == 0 and "deny" not in out
