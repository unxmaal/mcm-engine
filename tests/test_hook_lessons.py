"""PreToolUse lesson injection (#210)."""
from __future__ import annotations

import io
import json
import socket
import time
from pathlib import Path

import pytest

from mcm_engine.hooks import lessons as L
from mcm_engine.hooks import mcp_enforcement as me

# Search tool renders recorded from the live KB on 2026-11-10 (summary detail,
# trimmed to the hits each case needs plus the distractors that came with them).
PGREP_RULES = """[RULE #906] Correction to RULE #903: a session waiter must wait on a pid or a log line, never `pgrep -f <pattern>` (sibling waiters match each other, RULE #384) (workflow)
  HAPPENED 2026-10-19/20, sohot. Two run_in_background waiters each ran `while pgrep -f 'soh discover --loop' >/dev/null; do sleep 30; done` (one before a round 9 deploy).
  Match: each ran `while ,pgrep, -f 'soh discover --loop' >/dev/null; do ,sleep, 30; done

[RULE #176] A test that passes against known-broken code is the loudest signal the test is wrong (testing)
  # A test that passes against known-broken code is the loudest signal the test is wrong
  Match: decoy was: subprocess.Popen(["bash", "-c", ",sleep, 20"

(summary view — fetch any full body with get_entry("<type>", <id>); pass detail="full" to search to include all bodies inline.)"""

PGREP_NEGATIVES = """[NEGATIVE #48] shell: Background wait loops of the form `until ! pgrep -f "make check" >/dev/null; do sleep 10; done` never exit.
  Fix: Wait on the pid captured at launch.
  Match: until ! ,pgrep, -f "make check" >/dev/null; do ,sleep, 10"""

CHECKOUT_RULES = """[RULE #951] gitignore "node_modules/" does not match a symlink: a worktree's node_modules link gets committed, then checkouts clobber the real directory (tooling)
  FOUND 2026-10-20.
  Match: then ,checkouts, clobber the real directory"""

CHECKOUT_NEGATIVES = """[NEGATIVE #30] git-workflow: Committed a feature's work (#159) directly onto local main: after merging the previous PR I ran `git checkout main` and then started the next issue's edits without `git checkout -b`, so the commit landed on main.
  Fix: For each new issue, the FIRST action after syncing main is `git checkout -b <branch>` BEFORE any edit. N… (get_entry('negative', 30) for full)
  Match: merging the previous PR I ran `,git, ,checkout, main` and then started the next

[NEGATIVE #47] git-hygiene: sohot #1481: red-proofed by `git checkout origin/main -- F`, run tests, `git checkout HEAD -- F` while F had uncommitted edits (a mid-task rescope). The restore brought back the last commit, silently wiping the uncommitted edits.
  Why: `git checkout HEAD -- F` restores the committed blob, not the working-tree state before the revert.
  Fix: Commit (a WIP commit is fine) before any revert-and-restore red-proof, and check `git status --short` is clean first. Then the HEAD restore is exact.
  Match: hygiene sohot #1481: red-proofed by `,git, ,checkout, origin/main -- F`, run tests"""

ECHO_RULES = """[RULE #794] zsh: a bare word starting with = is EQUALS expansion; `echo ====` fails "=== not found" (shell)
  HAPPENED 2026-10-17 in a Claude Code Bash tool call.
  Match: word starting with = is EQUALS expansion; `,echo, ====` fails"""

EDIT_RULES = """[RULE #61] PreToolUse hook is fail-open: records consultation_gap, never blocks (issue #19) (mcm-engine)
  # PreToolUse hook is fail-open: records consultation_gap, never blocks (issue #19)
  Refines [[hook-mutator-only-block-and-event-log]]. The PreToolUse enforcement hook (`mcm-engine hook`, `src/mcm_engine/hooks/mcp_enforcement.py`) no longer blocks any edit.
  Match: PreToolUse ,hook, is fail-open"""

ENFORCEMENT_EDIT = {"tool_name": "Edit",
                    "tool_input": {"file_path": "/r/src/mcm_engine/hooks/mcp_enforcement.py"}}

PGREP_CMD = "while pgrep -f 'soh discover --loop' >/dev/null; do sleep 30; done"


def _fetch(*texts, calls=None):
    def run(query, cwd, timeout):
        if calls is not None:
            calls.append(query)
        return list(texts)
    return run


def _bash(cmd):
    return {"tool_name": "Bash", "tool_input": {"command": cmd}}


def test_pgrep_waiter_gets_rule_906(tmp_path):
    out = L.lessons_for("Bash", _bash(PGREP_CMD), {}, tmp_path,
                        fetch=_fetch(PGREP_RULES, PGREP_NEGATIVES))
    assert out.status == "injected"
    assert out.ids[0] == "rule:906"
    assert "RULE #906" in out.text
    assert "must wait on a pid or a log line, never `pgrep -f <pattern>`" in out.text
    assert "get_entry('rule', 906)" in out.text
    # The sleep-only distractor never reaches the agent.
    assert "rule:176" not in out.ids


def test_git_checkout_dashdash_gets_negative_47_first(tmp_path):
    out = L.lessons_for("Bash", _bash("git checkout -- f"), {}, tmp_path,
                        fetch=_fetch(CHECKOUT_RULES, CHECKOUT_NEGATIVES))
    assert out.status == "injected"
    assert out.ids[0] == "negative:47"
    assert "NEGATIVE #47" in out.text
    assert "Fix: Commit (a WIP commit is fine) before any revert-and-restore" in out.text
    assert "get_entry('negative', 47)" in out.text


def test_unrelated_command_gets_nothing_although_the_server_returned_hits(tmp_path):
    calls: list = []
    out = L.lessons_for("Bash", _bash("echo hello world"), {}, tmp_path,
                        fetch=_fetch(ECHO_RULES, calls=calls))
    assert calls == ["echo"]
    assert out.status == "none" and out.ids == [] and out.text == ""


def test_threshold_rejects_hits_that_do_not_share_the_actions_terms(tmp_path):
    out = L.lessons_for("Bash", _bash("cargo build --release"), {}, tmp_path,
                        fetch=_fetch(PGREP_RULES, CHECKOUT_NEGATIVES))
    assert out.status == "none" and out.text == ""


def test_same_entry_is_not_injected_twice_within_the_window(tmp_path):
    state: dict = {}
    fetch = _fetch(PGREP_RULES, PGREP_NEGATIVES)
    first = L.lessons_for("Bash", _bash(PGREP_CMD), state, tmp_path, fetch=fetch)
    second = L.lessons_for("Bash", _bash(PGREP_CMD), state, tmp_path, fetch=fetch)
    assert first.status == "injected" and "rule:906" in first.ids
    assert second.status == "dedup" and second.text == ""
    for _ in range(L.DEDUP_WINDOW_CALLS - 2):
        L.lessons_for("Bash", _bash("echo x"), state, tmp_path, fetch=_fetch())
    third = L.lessons_for("Bash", _bash(PGREP_CMD), state, tmp_path, fetch=fetch)
    assert third.status == "injected" and "rule:906" in third.ids


def test_dedup_shows_the_next_unseen_entry(tmp_path):
    state: dict = {"lessons_shown": {"rule:906": 1}, "lesson_calls": 1}
    out = L.lessons_for("Bash", _bash(PGREP_CMD), state, tmp_path,
                        fetch=_fetch(PGREP_RULES, PGREP_NEGATIVES))
    assert out.status == "injected" and "rule:906" not in out.ids
    assert "negative:48" in out.ids


def test_at_most_three_lessons(tmp_path):
    many = "\n\n".join(
        f"[NEGATIVE #{i}] shell: pgrep -f loop {i} with sleep\n  Fix: x" for i in range(1, 8))
    out = L.lessons_for("Bash", _bash(PGREP_CMD), {}, tmp_path, fetch=_fetch(many))
    assert len(out.ids) == L.MAX_INJECT


def test_slow_retrieval_is_abandoned_at_the_budget(tmp_path):
    def slow(query, cwd, timeout):
        time.sleep(5)
        return [PGREP_RULES]
    t0 = time.perf_counter()
    out = L.lessons_for("Bash", _bash(PGREP_CMD), {}, tmp_path, fetch=slow, budget=0.1)
    assert time.perf_counter() - t0 < 4
    assert out.status == "timeout" and out.text == ""


def test_retrieval_error_injects_nothing(tmp_path):
    def boom(query, cwd, timeout):
        raise ConnectionError("down")
    out = L.lessons_for("Bash", _bash(PGREP_CMD), {}, tmp_path, fetch=boom)
    assert out.status == "error" and out.text == ""


def test_server_down_returns_error_without_blocking(tmp_path, monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setenv("MCM_MCP_URL", f"http://127.0.0.1:{port}/mcp")
    out = L.lessons_for("Bash", _bash(PGREP_CMD), {}, tmp_path, budget=2.0)
    assert out.status in ("error", "timeout") and out.text == ""


def test_no_server_configured_is_not_eligible(tmp_path, monkeypatch):
    monkeypatch.delenv("MCM_MCP_URL", raising=False)
    assert L.lessons_for("Bash", _bash(PGREP_CMD), {}, tmp_path) is None


def test_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MCM_LESSONS", "0")
    assert L.lessons_for("Bash", _bash(PGREP_CMD), {}, tmp_path, fetch=_fetch(PGREP_RULES)) is None


def test_other_tools_are_not_eligible(tmp_path):
    ev = {"tool_input": {"file_path": "/x/y.py"}}
    assert L.lessons_for("Read", ev, {}, tmp_path, fetch=_fetch(PGREP_RULES)) is None


@pytest.mark.parametrize("tool,tool_input,expected", [
    ("Bash", {"command": PGREP_CMD}, ["pgrep", "sleep"]),
    ("Bash", {"command": "git checkout -- f"}, ["checkout", "git"]),
    ("Bash", {"command": "gh pr create --title x"}, ["create", "gh", "pr"]),
    ("Bash", {"command": "FOO=1 uv run python -m mcm_engine.hooks.lessons"},
     ["mcm_engine", "hooks", "lessons", "uv", "run"]),
    ("Bash", {"command": "pytest tests/test_hook_lessons.py -q 2>&1 > out.log"},
     ["pytest", "test_hook_lessons"]),
    ("Edit", {"file_path": "/r/src/mcm_engine/hooks/mcp_enforcement.py"},
     ["mcp_enforcement", "hooks", "mcm_engine"]),
])
def test_action_terms(tool, tool_input, expected):
    at = L.action_terms(tool, tool_input)
    assert at.query.split() == expected


def test_flags_and_strong_stems():
    at = L.action_terms("Bash", {"command": PGREP_CMD})
    assert {"-f", "--loop"} <= at.flags
    at = L.action_terms("Edit", {"file_path": "/r/src/mcm_engine/hooks/mcp_enforcement.py"})
    assert "mcp_enforcement" in at.strong


def _main(monkeypatch, capsys, event):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    rc = me.main([])
    return rc, capsys.readouterr()


def test_hook_main_injects_and_logs_ids(tmp_path, monkeypatch, capsys):
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("MCM_MCP_URL", "http://127.0.0.1:9/mcp")
    monkeypatch.setattr(L, "http_fetch", _fetch(EDIT_RULES))
    monkeypatch.setattr(me, "_gate_http_search", lambda q, c: "")
    # An Edit, so the deny table (#208) never sees it.
    ev = {**ENFORCEMENT_EDIT, "session_id": "s1", "cwd": str(tmp_path)}
    rc, cap = _main(monkeypatch, capsys, ev)
    assert rc == 0
    payload = json.loads(cap.out.strip().splitlines()[-1])
    hso = payload["hookSpecificOutput"]
    assert "permissionDecision" not in hso
    assert "RULE #61" in hso["additionalContext"]
    events = [json.loads(l) for l in
              (tmp_path / ".claude" / "mcp-enforcement-events.jsonl").read_text().splitlines()]
    lessons = [e for e in events if e["action"] == "lessons"]
    assert lessons and lessons[-1]["status"] == "injected"
    assert lessons[-1]["ids"] == ["rule:61"]
    assert lessons[-1]["query"] == "mcp_enforcement hooks mcm_engine"
    # Second identical call: logged as dedup, nothing injected.
    rc, cap = _main(monkeypatch, capsys, ev)
    assert "RULE #61" not in cap.out
    events = [json.loads(l) for l in
              (tmp_path / ".claude" / "mcp-enforcement-events.jsonl").read_text().splitlines()]
    assert [e for e in events if e["action"] == "lessons"][-1]["status"] == "dedup"


def test_hook_main_unrelated_command_prints_nothing(tmp_path, monkeypatch, capsys):
    (tmp_path / ".git").mkdir()
    monkeypatch.setenv("MCM_MCP_URL", "http://127.0.0.1:9/mcp")
    monkeypatch.setattr(L, "http_fetch", _fetch(ECHO_RULES))
    monkeypatch.setattr(me, "_gate_http_search", lambda q, c: "")
    ev = {**_bash("echo hello"), "session_id": "s1", "cwd": str(tmp_path)}
    rc, cap = _main(monkeypatch, capsys, ev)
    assert rc == 0 and cap.out == ""


def test_hook_main_fault_in_lessons_still_allows(tmp_path, monkeypatch, capsys):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(L, "lessons_for", lambda *a, **k: 1 / 0)
    ev = {**ENFORCEMENT_EDIT, "session_id": "s1", "cwd": str(tmp_path)}
    rc, cap = _main(monkeypatch, capsys, ev)
    assert rc == 0 and cap.out == ""
