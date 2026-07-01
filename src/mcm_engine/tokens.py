"""Bearer-token authentication for the HTTP/streamable-HTTP transport.

LODESTONE additive surface. The tokens table is created by the
Postgres adapter's DDL; this module hashes, mints, validates, and
revokes against that table.

Tokens are presented to clients as a 32-byte URL-safe random string
with a short prefix (``lst_``). Only the SHA-256 hash is stored —
the plaintext is shown to the operator once at mint time and never
again. A non-NULL ``revoked_at`` removes a token from the validate
path.

Auth is optional. Read the MCM_AUTH_REQUIRED env var: when "true",
every non-health request must carry an Authorization: Bearer <token>
header that hashes to a live row. When "false" (default for
backwards compatibility), the middleware passes everything through.
"""
from __future__ import annotations

import hashlib
import os
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import psycopg


_TOKEN_PREFIX = "lst_"


@dataclass(frozen=True)
class MintedToken:
    """Returned by mint_token. The plaintext is shown to the operator
    once and is not recoverable from the database."""

    plaintext: str
    principal: str


def _hash(plaintext: str) -> str:
    """Stable hash. The whole token (prefix included) is hashed so the
    prefix can change without invalidating existing tokens."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def mint_token(conn: "psycopg.Connection", principal: str) -> MintedToken:
    """Generate a new bearer token and write its hash to the tokens
    table. Returns the plaintext to display once.

    Raises ValueError on empty/whitespace principal.
    """
    if not principal or not principal.strip():
        raise ValueError("principal must be non-empty")
    principal = principal.strip()

    plaintext = _TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_hash = _hash(plaintext)

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tokens (token_hash, principal) VALUES (%s, %s)",
            (token_hash, principal),
        )
    conn.commit()
    return MintedToken(plaintext=plaintext, principal=principal)


def validate_token(conn: "psycopg.Connection", plaintext: str) -> Optional[str]:
    """Return the principal if the token is live, None otherwise.

    Touches last_used_at on success so an operator can audit token
    activity. Rolls back the read txn so the caller's connection
    isn't left ``idle in transaction``.
    """
    token_hash = _hash(plaintext)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, principal FROM tokens "
                "WHERE token_hash = %s AND revoked_at IS NULL",
                (token_hash,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            # psycopg's dict_row gives mappings; fall back to tuple
            # indexing for the row_factory=None case.
            token_id = row["id"] if hasattr(row, "keys") else row[0]
            principal = row["principal"] if hasattr(row, "keys") else row[1]
            cur.execute(
                "UPDATE tokens SET last_used_at = now() WHERE id = %s",
                (token_id,),
            )
        conn.commit()
        return principal
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


def revoke_token(conn: "psycopg.Connection", plaintext: str) -> bool:
    """Mark a token revoked. Returns True if a live token matched.

    Re-revoking a previously-revoked token is a no-op that returns
    False. The hash never changes so the plaintext alone is enough
    to identify the row.
    """
    token_hash = _hash(plaintext)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE tokens SET revoked_at = now() "
            "WHERE token_hash = %s AND revoked_at IS NULL",
            (token_hash,),
        )
        revoked = cur.rowcount
    conn.commit()
    return revoked > 0


def auth_required() -> bool:
    """True when MCM_AUTH_REQUIRED env var enables enforcement.

    Default is False so existing deployments (stdio, single-tenant
    daemons) are unaffected. LODESTONE's chart sets it to true.
    """
    return os.environ.get("MCM_AUTH_REQUIRED", "false").lower() in {
        "true", "1", "yes",
    }
