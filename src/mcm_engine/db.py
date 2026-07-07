"""KnowledgeDB — SQLite wrapper with WAL, write-retry, and FTS5 helpers."""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
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
        # >0 while a storage.transaction() block is open. Intra-block commit()
        # calls are deferred so a multi-write batch is one atomic unit; the
        # outermost end_transaction() performs the single real commit/rollback.
        self._tx_depth = 0
        # One re-entrant lock serializes ALL access to the single shared
        # connection (issue #83 hardening). Post-#79 one connection is shared
        # across every embedded adapter, and it is also driven by real
        # background threads (the watcher cascade's Timer threads). Without this
        # lock, `_tx_depth`, the implicit transaction, and `_reconnect`'s
        # connection swap are read-modify-written from multiple threads: a
        # concurrent write's commit() no-ops inside another thread's open
        # transaction (silent lost write / cross-thread rollback), and a
        # reconnect closes the connection out from under an in-flight statement.
        # Re-entrant so a transaction()'s inner writes on the same thread nest
        # freely; other threads block until the block completes.
        self._lock = threading.RLock()

    def _open_connection(self) -> sqlite3.Connection:
        """Open a fresh SQLite connection with correct pragmas.

        ``check_same_thread=False`` lets the HTTP/SSE daemon (MCM2-20)
        and the watcher cascade (MCM2-23) share a single connection
        across uvicorn worker threads and the watchdog Observer
        thread. SQLite still serializes writes internally via WAL +
        busy_timeout — the disabled guard only matters for read-only
        contention which WAL handles correctly.
        """
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _reconnect(self):
        """Close and reopen the connection. Called on readonly/locked errors."""
        with self._lock:
            log("Reconnecting to DB (previous connection went stale)")
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = self._open_connection()

    def execute_write(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a write statement with one retry on readonly/locked errors.

        The reconnect-and-retry is skipped inside a transaction() block: a
        reconnect opens a fresh connection and silently abandons the batch's
        prior writes. Inside a batch we let the lock error propagate so the
        transaction rolls back cleanly rather than committing a partial batch.
        """
        with self._lock:
            try:
                return self.conn.execute(sql, params)
            except sqlite3.OperationalError as e:
                err = str(e).lower()
                if ("readonly" in err or "locked" in err) and self._tx_depth == 0:
                    log(f"Write failed ({e}), reconnecting and retrying")
                    self._reconnect()
                    return self.conn.execute(sql, params)
                raise

    def commit(self):
        """Commit with one retry on readonly/locked errors.

        Deferred while a transaction() block is open — the outermost
        end_transaction(commit=True) performs the single real commit."""
        with self._lock:
            if self._tx_depth > 0:
                return
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

    def begin_transaction(self) -> None:
        """Enter a deferred-commit scope. Nestable via a depth counter.

        Prefer the ``transaction()`` context manager, which holds the
        connection lock across the whole block; this pair is retained for
        callers that manage begin/end explicitly."""
        with self._lock:
            self._tx_depth += 1

    def end_transaction(self, *, commit: bool) -> None:
        """Leave a deferred-commit scope. The outermost exit commits (or rolls
        back) the accumulated writes exactly once."""
        with self._lock:
            if self._tx_depth == 0:
                return
            self._tx_depth -= 1
            if self._tx_depth > 0:
                return
            if commit:
                self.commit()
            else:
                try:
                    self.conn.rollback()
                except Exception:
                    pass

    @contextmanager
    def transaction(self):
        """Group writes into one atomic unit, holding the connection lock for the
        ENTIRE block. This is what makes a multi-write batch safe against other
        threads (the watcher cascade; any future threaded dispatch): no other
        thread can execute a write, commit, or reconnect while the transaction is
        open, so a sibling write can't be folded into — and lost with — this
        block. Re-entrant, so nested transaction()/execute_write on the same
        thread work."""
        with self._lock:
            self.begin_transaction()
            try:
                yield
            except BaseException:
                self.end_transaction(commit=False)
                raise
            else:
                self.end_transaction(commit=True)

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute a read query."""
        with self._lock:
            return self.conn.execute(sql, params)

    def executescript(self, sql: str) -> None:
        """Execute a multi-statement SQL script."""
        with self._lock:
            self.conn.executescript(sql)

    def close(self):
        """Close the connection."""
        with self._lock:
            try:
                self.conn.close()
            except Exception:
                pass
