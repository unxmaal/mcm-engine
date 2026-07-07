"""Tests for the LODESTONE tokens module + bearer-token middleware
+ /v1/claims REST shim.

Runs only when MCM_TEST_POSTGRES_DSN is set, same gate as the rest
of the postgres-adapter suite.
"""
from __future__ import annotations

import os

import pytest

TEST_DSN = os.environ.get("MCM_TEST_POSTGRES_DSN")
pytestmark = pytest.mark.skipif(
    not TEST_DSN, reason="MCM_TEST_POSTGRES_DSN not set"
)


@pytest.fixture
def storage():
    from mcm_engine.adapters.postgres._pool import _active_conn
    from mcm_engine.adapters.postgres.storage import PostgresStorage

    store = PostgresStorage(dsn=TEST_DSN)
    store.ensure_schema()
    store.truncate_all()
    # These tests drive raw token SQL through ``storage._conn`` directly (the
    # daemon's out-of-band token/claims paths do the same). Under the pool,
    # ``_conn`` is the current call-chain's borrowed connection, so bind one for
    # the whole test — recreating the prior single-connection behavior. The
    # /v1/claims endpoint under TestClient borrows its OWN connection from the
    # pool (a different thread, so this binding doesn't leak into it).
    with store._pool.connection() as conn:
        token = _active_conn.set(conn)
        try:
            yield store
        finally:
            _active_conn.reset(token)
    store.close()


# -----------------------------------------------------------------------
# tokens module
# -----------------------------------------------------------------------


def test_mint_token_returns_plaintext_with_prefix(storage):
    from mcm_engine import tokens

    minted = tokens.mint_token(storage._conn, principal="alice")
    assert minted.plaintext.startswith("lst_")
    assert minted.principal == "alice"
    # 32 url-safe bytes = ~43 chars, plus the 4-char prefix.
    assert len(minted.plaintext) >= 40


def test_mint_token_writes_hashed_row(storage):
    from mcm_engine import tokens

    minted = tokens.mint_token(storage._conn, principal="alice")
    with storage._conn.cursor() as cur:
        cur.execute("SELECT token_hash, principal FROM tokens")
        rows = cur.fetchall()
    storage._conn.commit()
    assert len(rows) == 1
    row = rows[0]
    token_hash = row["token_hash"] if hasattr(row, "keys") else row[0]
    principal = row["principal"] if hasattr(row, "keys") else row[1]
    assert principal == "alice"
    # Plaintext is NOT stored.
    assert minted.plaintext not in token_hash
    # SHA-256 hex is 64 chars.
    assert len(token_hash) == 64


def test_mint_token_rejects_empty_principal(storage):
    from mcm_engine import tokens

    with pytest.raises(ValueError):
        tokens.mint_token(storage._conn, principal="")
    with pytest.raises(ValueError):
        tokens.mint_token(storage._conn, principal="   ")


def test_validate_token_returns_principal(storage):
    from mcm_engine import tokens

    minted = tokens.mint_token(storage._conn, principal="bob")
    assert tokens.validate_token(storage._conn, minted.plaintext) == "bob"


def test_validate_token_rejects_unknown(storage):
    from mcm_engine import tokens

    assert tokens.validate_token(storage._conn, "lst_never-minted") is None


def test_validate_token_touches_last_used_at(storage):
    from mcm_engine import tokens

    minted = tokens.mint_token(storage._conn, principal="alice")
    with storage._conn.cursor() as cur:
        cur.execute("SELECT last_used_at FROM tokens")
        before = cur.fetchone()
    storage._conn.commit()
    before_val = before["last_used_at"] if hasattr(before, "keys") else before[0]
    assert before_val is None

    tokens.validate_token(storage._conn, minted.plaintext)

    with storage._conn.cursor() as cur:
        cur.execute("SELECT last_used_at FROM tokens")
        after = cur.fetchone()
    storage._conn.commit()
    after_val = after["last_used_at"] if hasattr(after, "keys") else after[0]
    assert after_val is not None


def test_revoke_token_blocks_further_validation(storage):
    from mcm_engine import tokens

    minted = tokens.mint_token(storage._conn, principal="alice")
    assert tokens.revoke_token(storage._conn, minted.plaintext) is True
    assert tokens.validate_token(storage._conn, minted.plaintext) is None
    # Re-revoking is a no-op.
    assert tokens.revoke_token(storage._conn, minted.plaintext) is False


def test_auth_required_env(monkeypatch):
    from mcm_engine import tokens

    monkeypatch.delenv("MCM_AUTH_REQUIRED", raising=False)
    assert tokens.auth_required() is False
    monkeypatch.setenv("MCM_AUTH_REQUIRED", "true")
    assert tokens.auth_required() is True
    monkeypatch.setenv("MCM_AUTH_REQUIRED", "1")
    assert tokens.auth_required() is True
    monkeypatch.setenv("MCM_AUTH_REQUIRED", "no")
    assert tokens.auth_required() is False


# -----------------------------------------------------------------------
# /v1/claims endpoint + bearer-token middleware
# -----------------------------------------------------------------------


class _FakeServer:
    """Minimal stand-in for MCMServer. The middleware + claims endpoint
    only ever touch server.ctx.storage._conn."""

    class _Ctx:
        def __init__(self, storage):
            self.storage = storage

    def __init__(self, storage):
        self.ctx = self._Ctx(storage)
        # Empty MCP app stand-in: we don't exercise the MCP transport
        # in these tests, only the operational + claims routes.
        class _FakeMcp:
            def sse_app(self_inner):
                from starlette.applications import Starlette
                return Starlette()

            def streamable_http_app(self_inner):
                from starlette.applications import Starlette
                return Starlette()

        self.mcp = _FakeMcp()


@pytest.fixture
def client(storage):
    from starlette.testclient import TestClient

    from mcm_engine.transport import build_asgi_app

    server = _FakeServer(storage)
    app = build_asgi_app(server, transport="sse")
    return TestClient(app)


def test_healthz_open_without_auth(client, monkeypatch):
    monkeypatch.setenv("MCM_AUTH_REQUIRED", "true")
    resp = client.get("/healthz")
    assert resp.status_code == 200


def test_claims_requires_bearer_when_auth_required(client, monkeypatch):
    monkeypatch.setenv("MCM_AUTH_REQUIRED", "true")
    resp = client.post("/v1/claims", json={"claim": "hello"})
    assert resp.status_code == 401


def test_claims_accepts_valid_token(client, storage, monkeypatch):
    from mcm_engine import tokens

    monkeypatch.setenv("MCM_AUTH_REQUIRED", "true")
    minted = tokens.mint_token(storage._conn, principal="sieve")

    resp = client.post(
        "/v1/claims",
        json={
            "claim": "Postgres needs check_same_thread=False under ThreadPoolExecutor",
            "subject_keys": ["python", "sqlite"],
            "governance_tags": ["Internal"],
            "scope": "engineering",
            "provenance": [{"source": "memory", "ref": "rule:tdd"}],
        },
        headers={"Authorization": f"Bearer {minted.plaintext}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert "id" in body
    assert body["principal"] == "sieve"

    with storage._conn.cursor() as cur:
        cur.execute(
            "SELECT subject_keys, governance_tags, scope, status, provenance "
            "FROM knowledge WHERE id = %s",
            (body["id"],),
        )
        row = cur.fetchone()
    storage._conn.commit()
    cols = row if not hasattr(row, "keys") else (
        row["subject_keys"], row["governance_tags"], row["scope"],
        row["status"], row["provenance"],
    )
    assert cols[0] == ["python", "sqlite"]
    assert cols[1] == ["Internal"]
    assert cols[2] == "engineering"
    assert cols[3] == "active"
    assert cols[4] == [{"source": "memory", "ref": "rule:tdd"}]


def test_claims_rejects_invalid_token(client, monkeypatch):
    monkeypatch.setenv("MCM_AUTH_REQUIRED", "true")
    resp = client.post(
        "/v1/claims",
        json={"claim": "hi"},
        headers={"Authorization": "Bearer lst_not-real"},
    )
    assert resp.status_code == 401


def test_claims_rejects_revoked_token(client, storage, monkeypatch):
    from mcm_engine import tokens

    monkeypatch.setenv("MCM_AUTH_REQUIRED", "true")
    minted = tokens.mint_token(storage._conn, principal="paul")
    tokens.revoke_token(storage._conn, minted.plaintext)
    resp = client.post(
        "/v1/claims",
        json={"claim": "hi"},
        headers={"Authorization": f"Bearer {minted.plaintext}"},
    )
    assert resp.status_code == 401


def test_claims_passes_through_when_auth_disabled(client, monkeypatch):
    monkeypatch.delenv("MCM_AUTH_REQUIRED", raising=False)
    resp = client.post("/v1/claims", json={"claim": "open-bar mode"})
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["principal"] == "anonymous"


def test_claims_rejects_empty_claim(client, monkeypatch):
    monkeypatch.delenv("MCM_AUTH_REQUIRED", raising=False)
    resp = client.post("/v1/claims", json={"claim": "   "})
    assert resp.status_code == 400


def test_claims_rejects_bad_schema(client, monkeypatch):
    monkeypatch.delenv("MCM_AUTH_REQUIRED", raising=False)
    resp = client.post(
        "/v1/claims",
        json={"claim": "hi", "subject_keys": "not-a-list"},
    )
    assert resp.status_code == 400


# -----------------------------------------------------------------------
# kb_recall — exercised via direct SQL since the MCP-registered tool
# function lives inside a closure. The tool body uses exactly these
# statements; testing them here proves the recall semantics without
# spinning up a full MCMServer.
# -----------------------------------------------------------------------


def test_kb_recall_deletes_claim_and_writes_log(storage):
    conn = storage._conn

    # Insert a claim using the same INSERT shape /v1/claims uses.
    import json as _json
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO knowledge (topic, kind, summary, subject_keys, status, provenance)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            ("recall-test", "finding", "doomed claim", ["t"], "active", _json.dumps([])),
        )
        row = cur.fetchone()
    conn.commit()
    claim_id = row["id"] if hasattr(row, "keys") else row[0]

    # Recall semantics (mirroring the tool body).
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recall_log (claim_id, principal, reason) VALUES (%s, %s, %s)",
            (claim_id, "governance", "test"),
        )
        cur.execute("DELETE FROM knowledge WHERE id = %s", (claim_id,))
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM knowledge WHERE id = %s", (claim_id,))
        gone = cur.fetchone()
        cur.execute("SELECT principal, reason FROM recall_log WHERE claim_id = %s", (claim_id,))
        log_row = cur.fetchone()
    conn.commit()

    assert gone is None
    assert log_row is not None
    log_principal = log_row["principal"] if hasattr(log_row, "keys") else log_row[0]
    log_reason = log_row["reason"] if hasattr(log_row, "keys") else log_row[1]
    assert log_principal == "governance"
    assert log_reason == "test"


def test_kb_recall_log_persists_after_delete(storage):
    """Demonstrates that recall_log is independent of knowledge."""
    conn = storage._conn

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO knowledge (topic, kind, summary) VALUES (%s, %s, %s) RETURNING id",
            ("dead-letter", "finding", "to be recalled"),
        )
        row = cur.fetchone()
    conn.commit()
    claim_id = row["id"] if hasattr(row, "keys") else row[0]

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO recall_log (claim_id, principal, reason) VALUES (%s, %s, %s)",
            (claim_id, "alice", "operator request"),
        )
        cur.execute("DELETE FROM knowledge WHERE id = %s", (claim_id,))
    conn.commit()

    # Even though the FK-shaped relationship is gone, the log row
    # remains. recall_log.claim_id has no FK constraint by design.
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) AS n FROM recall_log WHERE claim_id = %s", (claim_id,))
        n_row = cur.fetchone()
    conn.commit()
    n = n_row["n"] if hasattr(n_row, "keys") else n_row[0]
    assert n == 1
