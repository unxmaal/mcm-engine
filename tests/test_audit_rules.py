"""Pure-logic tests for scripts/audit_rules.py (c5 Phase 1).

The MCP transport can't be exercised without a live pod, so we test the
parse/classify/render functions against a synthetic list_rules table.
"""
from __future__ import annotations

import importlib.util
import pathlib

import pytest

_PATH = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "audit_rules.py"
_spec = importlib.util.spec_from_file_location("audit_rules", _PATH)
audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(audit)


SAMPLE = """# rules — id | importance | scope | kind | category | hits | reinf | correct/incorrect | status | title
#12 | imp=2 | universal | directive | deploy | h=5 | rf=3 | 2/0 | active | Use uv for all Python operations
#7 | imp=0 | conditional | fact | mcp | h=0 | rf=0 | 0/0 | active | FastMCP localhost-only DNS-rebinding trap
#99 | imp=1 | conditional | directive | style | h=0 | rf=0 | 0/0 | active | Some unused directive rule
#50 | imp=0 | conditional | fact | x | h=2 | rf=0 | 0/0 | active | Always run migrations before deploy
#5 | imp=0 | conditional | fact | x | h=1 | rf=4 | 0/0 | active | Title with | a pipe
"""


def _by_id(rules):
    return {r["id"]: r for r in rules}


def test_parse_list_rules_fields_and_pipe_title():
    rules = audit.parse_list_rules(SAMPLE)
    assert len(rules) == 5
    r = _by_id(rules)
    assert r[12]["kind"] == "directive" and r[12]["importance"] == 2
    assert r[12]["hits"] == 5 and r[12]["reinf"] == 3
    assert r[7]["category"] == "mcp"
    # pipe inside the title survives the split
    assert r[5]["title"] == "Title with | a pipe"


def test_parse_ignores_header_and_blank():
    assert audit.parse_list_rules("# rules — id | ...\n\n") == []


@pytest.mark.parametrize("rid,expected", [
    (12, "KEEP"),        # directive, used, title matches
    (99, "DEMOTE"),      # directive, unused, importance>=1
    (50, "RECLASSIFY"),  # kind=fact but title imperative
    (5, "KEEP"),         # fact, reinforcement>=3 (load-bearing)
    (7, "REVIEW"),       # fact, unused
])
def test_classify(rid, expected):
    r = _by_id(audit.parse_list_rules(SAMPLE))[rid]
    rec, reasons = audit.classify(r)
    assert rec == expected
    assert reasons  # always explains itself


def test_unused_low_importance_directive_is_delete():
    r = _by_id(audit.parse_list_rules(
        "#3 | imp=0 | conditional | directive | x | h=0 | rf=0 | 0/0 | active | Some rule"))[3]
    assert audit.classify(r)[0] == "DELETE"


def test_render_report_has_table_and_summary():
    report = audit.render_report(audit.parse_list_rules(SAMPLE))
    assert "# Rules audit" in report
    assert "Total rules: 5" in report
    assert "| id | kind |" in report
    assert "**DEMOTE**" in report and "**RECLASSIFY**" in report
