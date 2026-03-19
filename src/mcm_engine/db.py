"""KnowledgeDB — SQLite wrapper with WAL, write-retry, and FTS5 helpers."""
from __future__ import annotations

import sqlite3
import time
from pathlib import Path

# --- Logging (file only, never stderr) ---
_log_file = None
_log_path: str | None = None


def set_log_path(path: str) -> None:
    global _log_path
    _log_path = path


def log(msg: str) -> None:
    """Log to file only. Never write to stderr — MCP clients treat stderr as failure."""
    global _log_file
    if _log_path is None:
        return
    try:
        if _log_file is None:
            _log_file = open(_log_path, "a")
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        _log_file.write(f"[{ts}] {msg}\n")
        _log_file.flush()
    except Exception:
        pass


def sanitize_fts(query: str) -> str:
    """Quote each term so FTS5 special chars (hyphens, colons) don't break queries."""
    terms = query.split()
    return " ".join(f'"{t}"' for t in terms)


# Noise words filtered from search queries
_NOISE_WORDS = frozenset({
    "the", "a", "an", "in", "on", "at", "to", "for", "of", "with",
    "is", "are", "was", "were", "be", "been", "being",
    "and", "or", "but", "not", "no", "this", "that",
    "how", "what", "when", "where", "why", "which",
    "it", "its", "my", "our", "your",
})


def build_fts_queries(query: str) -> list[str]:
    """Build FTS5 queries from most to least strict.

    Returns queries to try in order:
    1. AND of all significant terms (most precise)
    2. OR of all significant terms (any term matches)
    3. Prefix match on terms >= 3 chars (partial words)

    Porter stemming in the FTS5 index handles inflections automatically.
    """
    raw_terms = query.split()
    terms = [t for t in raw_terms if t.lower() not in _NOISE_WORDS and len(t) > 1]
    if not terms:
        terms = raw_terms  # fallback: use everything if all filtered

    if not terms:
        return []

    quoted = [f'"{t}"' for t in terms]

    queries = []
    if len(terms) > 1:
        # 1. AND: all terms must appear
        queries.append(" ".join(quoted))
        # 2. OR: any term matches
        queries.append(" OR ".join(quoted))
    else:
        queries.append(quoted[0])

    # 3. Prefix match on each term (catches partial words)
    prefix_terms = [f'"{t}"*' for t in terms if len(t) >= 3]
    if prefix_terms and len(terms) > 1:
        queries.append(" OR ".join(prefix_terms))

    return queries


def build_like_patterns(query: str) -> list[str]:
    """Build per-term LIKE patterns for fallback search.

    Returns individual '%term%' patterns for each significant word,
    instead of searching for the entire query as a substring.
    """
    terms = [t for t in query.split() if len(t) > 2]
    if not terms:
        terms = query.split()
    return [f"%{t}%" for t in terms if t]


class KnowledgeDB:
    """SQLite wrapper for all knowledge DB operations.

    Features:
    - WAL journal mode for concurrent readers + single writer
    - busy_timeout=5000 to handle brief concurrent access
    - Write retry on readonly/locked errors (reconnect + retry once)
    - FTS5 query sanitization
    """

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = self._open_connection()

    def _open_connection(self) -> sqlite3.Connection:
        """Open a fresh SQLite connection with correct pragmas."""
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _reconnect(self):
        """Close and reopen the connection. Called on readonly/locked errors."""
        log("Reconnecting to DB (previous connection went stale)")
        try:
            self.conn.close()
        except Exception:
            pass
        self.conn = self._open_connection()

    def execute_write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a write statement with one retry on readonly/locked errors."""
        try:
            return self.conn.execute(sql, params)
        except sqlite3.OperationalError as e:
            err = str(e).lower()
            if "readonly" in err or "locked" in err:
                log(f"Write failed ({e}), reconnecting and retrying")
                self._reconnect()
                return self.conn.execute(sql, params)
            raise

    def commit(self):
        """Commit with one retry on readonly/locked errors."""
        try:
            self.conn.commit()
        except sqlite3.OperationalError as e:
            err = str(e).lower()
            if "readonly" in err or "locked" in err:
                log(f"Commit failed ({e}), reconnecting and retrying")
                self._reconnect()
                # Re-raise — the transaction was lost, caller needs to re-execute
                raise
            raise

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a read query."""
        return self.conn.execute(sql, params)

    def executescript(self, sql: str) -> None:
        """Execute a multi-statement SQL script."""
        self.conn.executescript(sql)

    def close(self):
        """Close the connection."""
        try:
            self.conn.close()
        except Exception:
            pass
