"""Admin plane HTTP app (issue #64, Phase 3) — stdlib server, in-process.

Spins the real ThreadingHTTPServer on an ephemeral port and drives it with
urllib, so the routing + JSON contract is exercised end to end without a
browser or a framework.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import pytest

from mcm_engine.admin.app import make_server
from mcm_engine.adapters.sqlite.storage import SqliteStorage
from mcm_engine.backends import EntityType, RuleRow


@pytest.fixture
def server(db):
    storage = SqliteStorage(db=db)
    storage.insert_rule(RuleRow(id=0, title="uv rule", keywords="k", content="use uv"))
    httpd = make_server(storage, host="127.0.0.1", port=0)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield {"base": f"http://127.0.0.1:{port}", "storage": storage}
    httpd.shutdown()
    httpd.server_close()


def _get(url):
    with urllib.request.urlopen(url, timeout=5) as r:
        return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8")


def _post(url, obj):
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_index_serves_html_grid(server):
    status, ctype, body = _get(server["base"] + "/")
    assert status == 200
    assert "text/html" in ctype
    # the grid + its live-poll wiring are present
    assert "KB Rules" in body
    assert "/api/rules" in body
    assert "setInterval" in body


def test_index_has_nonclobbering_poll_and_coordinated_sticky(server):
    """Regression guard for the three Phase-3 UI defects:

      1. sticky thead overlapped rows because a wrapper's overflow made a
         nested scroll context — the JS-driven --head-top offset replaces the
         hardcoded guess and there is no overflow wrapper.
      2/3. the 2s poll rebuilt every row (tbody.innerHTML = "") and destroyed
         the control being edited, so `kind` changes and `category` typing were
         lost. The poll now reconciles keyed rows and skips document.activeElement.
    """
    _, _, body = _get(server["base"] + "/")
    # Non-destructive poll: no blanket tbody wipe; keyed reconciliation + a
    # focus guard so an in-progress edit is never clobbered.
    assert 'innerHTML = ""' not in body
    assert "rowsById" in body
    assert "activeElement" in body
    # Coordinated sticky: dynamic header offset, no overflow-wrapper scroll context.
    assert "--head-top" in body
    assert "overflow-x: auto" not in body


def test_api_rules_returns_payload(server):
    status, ctype, body = _get(server["base"] + "/api/rules")
    assert status == 200 and "application/json" in ctype
    payload = json.loads(body)
    assert payload["count"] == 1
    assert payload["rules"][0]["title"] == "uv rule"
    assert "vocab" in payload


def test_post_metadata_updates_rule(server):
    rid = server["storage"].find_rule_by_title("uv rule").id
    status, body = _post(server["base"] + f"/api/rules/{rid}/metadata",
                          {"importance": 2, "scope": "universal", "kind": "directive"})
    assert status == 200
    assert body["rule"]["importance"] == 2
    r = server["storage"].find_by_id(EntityType.RULE, rid)
    assert (r.scope, r.kind) == ("universal", "directive")


def test_post_metadata_invalid_returns_400(server):
    rid = server["storage"].find_rule_by_title("uv rule").id
    status, body = _post(server["base"] + f"/api/rules/{rid}/metadata",
                         {"scope": "galactic"})
    assert status == 400 and "error" in body
    assert server["storage"].find_by_id(EntityType.RULE, rid).scope == "conditional"


def test_post_metadata_unknown_rule_returns_404(server):
    status, body = _post(server["base"] + "/api/rules/999999/metadata",
                         {"importance": 1})
    assert status == 404


def test_unknown_path_404(server):
    try:
        _get(server["base"] + "/nope")
        assert False, "expected 404"
    except urllib.error.HTTPError as e:
        assert e.code == 404
