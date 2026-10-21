"""UserPromptSubmit hook: task-aware retrieval + inject (issue #157).

The classifier is deterministic (no LLM, DECISION #39): a prompt that references
history triggers a bounded KB search whose result is injected as
additionalContext, and the topic is recorded as covered in the shared hook state
(hooks/coverage.py). A non-historical prompt is a fast no-op. Fail-open (#61).
"""
from __future__ import annotations

import io
import json

import pytest

from mcm_engine.hooks import coverage
from mcm_engine.hooks.mcp_enforcement import _read_state, _state_path
from mcm_engine.hooks.user_prompt_submit import (
    INJECT_MAX_BYTES,
    _pick_keyword,
    _truncate_injection,
    classify,
    main,
)


# --- classifier -------------------------------------------------------------


@pytest.mark.parametrize("prompt", [
    "can you do that again like last time",
    "we decided to use postgres here, right?",
    "you said the healthcheck was on 8090 earlier",
    "still failing with the same error",
    "continue where we left off",
    "why did we cap the mcp dependency",
    "what's our convention for staging files",
])
def test_classify_detects_historical(prompt):
    is_hist, query = classify(prompt)
    assert is_hist is True


@pytest.mark.parametrize("prompt", [
    "add a function that reverses a string",
    "what is the capital of France",
    "write a test for the parser",
    "refactor this loop",
    "",
])
def test_classify_rejects_non_historical(prompt):
    is_hist, _ = classify(prompt)
    assert is_hist is False


def test_history_signal_respects_word_boundary():
    # "distill" contains "still" but must not trip the signal.
    assert classify("distill this document") == (False, "")


def test_pick_keyword_prefers_identifier():
    assert _pick_keyword("we changed the save_snapshot behavior again") == "save_snapshot"
    assert _pick_keyword("remember the MCM_PORT value we used") == "mcm_port"


def test_pick_keyword_longest_non_stopword():
    assert _pick_keyword("we discussed the healthcheck before") == "healthcheck"


def test_pick_keyword_empty_when_nothing_distinctive():
    assert _pick_keyword("we did it again") == ""


# --- main() wiring ----------------------------------------------------------


def _run(monkeypatch, capsys, event, *, search=None):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    rc = main(search=search)
    return rc, capsys.readouterr().out


def test_non_historical_prompt_is_fast_noop(monkeypatch, capsys, tmp_path):
    calls = []

    def fake_search(q, cwd):
        calls.append(q)
        return "SHOULD NOT RUN"

    rc, out = _run(
        monkeypatch, capsys,
        {"prompt": "write a parser", "cwd": str(tmp_path)},
        search=fake_search,
    )
    assert rc == 0
    assert out.strip() == ""  # nothing injected
    assert calls == []  # no search issued


def test_historical_prompt_injects_and_records_coverage(monkeypatch, capsys, tmp_path):
    def fake_search(q, cwd):
        assert q == "save_snapshot"
        return "[RULE #156] coverage ledger ..."

    rc, out = _run(
        monkeypatch, capsys,
        {"prompt": "why did we change save_snapshot again", "cwd": str(tmp_path),
         "session_id": "s1"},
        search=fake_search,
    )
    assert rc == 0
    payload = json.loads(out)
    hso = payload["hookSpecificOutput"]
    assert hso["hookEventName"] == "UserPromptSubmit"
    assert "save_snapshot" in hso["additionalContext"]
    assert "[RULE #156]" in hso["additionalContext"]

    # coverage recorded under the agent key in the shared state file
    state = _read_state(_state_path(tmp_path))
    entry = state["s1"]
    assert coverage.is_covered(entry, "save_snapshot") is True


def test_coverage_recorded_even_when_search_empty(monkeypatch, capsys, tmp_path):
    rc, out = _run(
        monkeypatch, capsys,
        {"prompt": "we discussed the healthcheck issue earlier", "cwd": str(tmp_path),
         "session_id": "s2"},
        search=lambda q, cwd: "",  # no hits
    )
    assert rc == 0
    assert out.strip() == ""  # nothing to inject
    entry = _read_state(_state_path(tmp_path))["s2"]
    assert coverage.is_covered(entry, "healthcheck") is True


def test_search_exception_is_fail_open(monkeypatch, capsys, tmp_path):
    def boom(q, cwd):
        raise RuntimeError("gateway down")

    rc, out = _run(
        monkeypatch, capsys,
        {"prompt": "continue the save_snapshot work", "cwd": str(tmp_path)},
        search=boom,
    )
    assert rc == 0
    assert out.strip() == ""


def test_bad_stdin_is_fail_open(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("not json{"))
    assert main() == 0
    assert capsys.readouterr().out.strip() == ""


# --- issue #172: byte cap + session dedup -----------------------------------


def test_truncate_injection_bounds_bytes_at_hit_boundary():
    hits = "\n\n".join(f"[NEGATIVE #{i}] " + "X" * 500 for i in range(10))
    out = _truncate_injection(hits, "deploy")
    assert len(out.encode("utf-8")) <= INJECT_MAX_BYTES
    assert "truncated — run search('deploy')" in out
    # cut on a boundary: no hit is shown half (every kept [NEGATIVE #n] line is
    # followed by its full 500-X body up to the pointer)
    assert out.endswith("for the rest)")


def test_truncate_injection_passes_small_result_through():
    small = "[RULE #1] tiny"
    assert _truncate_injection(small, "q") == small


def test_huge_injection_is_capped_in_main(monkeypatch, capsys, tmp_path):
    big = "\n\n".join(f"[NEGATIVE #{i}] " + "Y" * 800 for i in range(20))
    rc, out = _run(
        monkeypatch, capsys,
        {"prompt": "the deploy_guard thing again", "cwd": str(tmp_path),
         "session_id": "cap"},
        search=lambda q, cwd: big,
    )
    assert rc == 0
    hso = json.loads(out)["hookSpecificOutput"]
    # the injected result body is bounded (prefix adds a little, allow slack)
    assert len(hso["additionalContext"].encode("utf-8")) <= INJECT_MAX_BYTES + 200
    assert "truncated" in hso["additionalContext"]


def test_topic_injected_once_per_session(monkeypatch, capsys, tmp_path):
    runs = []

    def fake_search(q, cwd):
        runs.append(q)
        return "[RULE #1] the answer for save_snapshot"

    ev = {"prompt": "why did we change save_snapshot again", "cwd": str(tmp_path),
          "session_id": "dedup"}
    rc1, out1 = _run(monkeypatch, capsys, ev, search=fake_search)
    rc2, out2 = _run(monkeypatch, capsys, ev, search=fake_search)

    assert "[RULE #1]" in json.loads(out1)["hookSpecificOutput"]["additionalContext"]
    assert out2.strip() == ""           # second time: no re-injection


def test_different_topic_still_injects_same_session(monkeypatch, capsys, tmp_path):
    def fake_search(q, cwd):
        return f"[RULE #1] answer for {q}"

    base = {"cwd": str(tmp_path), "session_id": "multi"}
    _run(monkeypatch, capsys,
         {**base, "prompt": "why did we change save_snapshot again"},
         search=fake_search)
    _, out2 = _run(monkeypatch, capsys,
                   {**base, "prompt": "remember the healthcheck we discussed"},
                   search=fake_search)
    assert "healthcheck" in json.loads(out2)["hookSpecificOutput"]["additionalContext"]
