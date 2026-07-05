"""Files-win watcher cascade implementation (MCM2-23).

The watcher mirrors `rules/**/*.md` into a `StorageBackend`. Started by
the HTTP/SSE daemon (`mcm-engine serve`) — stdio mode does a one-shot
sync_rules at startup instead.

See docs/watcher-cascade.md for the conflict resolution rules and the
content-hash debounce explanation.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from watchdog.events import (
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEventHandler,
)
from watchdog.observers import Observer

from ..backends import EntityType, RuleRow, StorageBackend
from ..db import log
from ..destructive import archive_would_storm
from ..rules_links import build_wikilink_relations


# ---------------------------------------------------------------------------
# Hashing + parsing helpers
# ---------------------------------------------------------------------------


def compute_content_hash(text: str) -> str:
    """Stable content hash for the watcher's no-op-cascade check."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_rule_file(path: Path) -> Optional[dict[str, Any]]:
    """Pull title/keywords/category/description out of a markdown rule
    file. Returns None if there's no title (we won't track an
    unidentifiable file).
    """
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None

    result: dict[str, Any] = {"content_hash": compute_content_hash(content)}
    lines = content.split("\n")

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            result["title"] = stripped[2:].strip()
            break

    for line in lines:
        stripped = line.strip()
        m = re.match(r"\*\*Keywords?:\*\*\s*(.+)", stripped, re.IGNORECASE)
        if m:
            result["keywords"] = m.group(1).strip()
            continue
        m = re.match(r"\*\*Category:\*\*\s*(.+)", stripped, re.IGNORECASE)
        if m:
            result["category"] = m.group(1).strip()
            continue

    in_body = False
    desc_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not in_body:
            if stripped.startswith("# ") and not stripped.startswith("## "):
                continue
            if re.match(r"\*\*(Keywords?|Category):\*\*", stripped, re.IGNORECASE):
                continue
            if stripped == "":
                continue
            in_body = True
        if in_body:
            if stripped == "" and desc_lines:
                break
            if stripped.startswith("## "):
                break
            desc_lines.append(stripped)
    if desc_lines:
        result["description"] = " ".join(desc_lines)

    if "title" not in result:
        return None
    return result


# ---------------------------------------------------------------------------
# RulesWatcher
# ---------------------------------------------------------------------------


class _RulesEventHandler(FileSystemEventHandler):
    """Watchdog event handler that forwards .md events to the cascade."""

    def __init__(self, watcher: "RulesWatcher"):
        self._watcher = watcher

    def _is_rule(self, path: str) -> bool:
        return path.endswith(".md") and not Path(path).name.startswith(".")

    def on_created(self, event):
        if not isinstance(event, FileCreatedEvent):
            return
        if event.is_directory:
            return
        if self._is_rule(event.src_path):
            self._watcher._schedule(event.src_path, "upsert")

    def on_modified(self, event):
        if not isinstance(event, FileModifiedEvent):
            return
        if event.is_directory:
            return
        if self._is_rule(event.src_path):
            self._watcher._schedule(event.src_path, "upsert")

    def on_deleted(self, event):
        if not isinstance(event, FileDeletedEvent):
            return
        if event.is_directory:
            return
        if self._is_rule(event.src_path):
            self._watcher._schedule(event.src_path, "delete")

    def on_moved(self, event):
        if not isinstance(event, FileMovedEvent):
            return
        if event.is_directory:
            return
        # Delete-old + create-new per docs/watcher-cascade.md.
        if self._is_rule(event.src_path):
            self._watcher._schedule(event.src_path, "delete")
        if self._is_rule(event.dest_path):
            self._watcher._schedule(event.dest_path, "upsert")


class RulesWatcher:
    """Watches a rules directory and cascades changes into a StorageBackend.

    Lifecycle:
        watcher = RulesWatcher(storage, rules_path, project_root)
        watcher.sync_once()      # bring DB current with disk
        watcher.start()          # spawn background thread
        ...                      # serves the daemon
        watcher.stop()           # join the thread

    For stdio mode, call ``sync_once()`` only; never ``start()``.
    """

    def __init__(
        self,
        storage: StorageBackend,
        rules_path: Path,
        project_root: Path,
        *,
        debounce_ms: int = 500,
        archive_circuit_floor: int = 5,
        archive_circuit_fraction: float = 0.5,
    ):
        self._storage = storage
        self._rules_path = Path(rules_path).resolve()
        self._project_root = Path(project_root).resolve()
        self._debounce_s = debounce_ms / 1000.0
        # Layer 3 blast-radius guard (issue #16): refuse to archive more than
        # `fraction` of managed rules in one sweep once the count exceeds
        # `floor`. A suddenly-empty rules dir (failed mount, mid-checkout, a
        # pod with no filesystem) should not cascade into a mass wipe.
        self._archive_circuit_floor = archive_circuit_floor
        self._archive_circuit_fraction = archive_circuit_fraction
        self._observer: Optional[Any] = None
        self._handler = _RulesEventHandler(self)
        # Per-path debounce state. Maps absolute path → (timer, op).
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()
        # For diagnostics + tests.
        self._cascade_count: int = 0

    # ---- public API ----

    def start(self) -> None:
        """Spawn the watchdog observer thread. Daemon-mode only."""
        if self._observer is not None:
            return
        self._rules_path.mkdir(parents=True, exist_ok=True)
        obs = Observer()
        obs.schedule(self._handler, str(self._rules_path), recursive=True)
        obs.start()
        self._observer = obs

    def stop(self) -> None:
        """Halt the observer and drain any pending debounced events."""
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        # Cancel any pending timers.
        with self._lock:
            for t in self._timers.values():
                t.cancel()
            self._timers.clear()

    def sync_once(self) -> dict[str, int]:
        """One-shot sync: bring DB current with everything on disk now.

        For each .md file on disk, ensure a row exists matching its
        content. For each row whose file is missing, soft-delete it.

        Returns a small dict of counts for diagnostics.
        """
        counts = {"upserted": 0, "archived": 0, "unchanged": 0, "links": 0,
                  "archive_blocked": 0}
        if not self._rules_path.exists():
            return counts

        seen_paths: set[str] = set()
        for md in sorted(self._rules_path.rglob("*.md")):
            rel = self._rel_path(md)
            seen_paths.add(rel)
            action = self._cascade_upsert(md)
            if action == "upserted":
                counts["upserted"] += 1
            else:
                counts["unchanged"] += 1

        # Soft-delete orphans — but only files THIS watcher manages, i.e. whose
        # file_path lives under rules_path (Layer 2). A provenance path or a
        # DB-native import (file_path elsewhere or None) is not ours to reap.
        managed = [
            r for r in self._storage.list_rules_with_file_paths()
            if r.file_path and not r.archived and self._is_managed_path(r.file_path)
        ]
        orphans = [r for r in managed if r.file_path not in seen_paths]

        # Layer 3 circuit breaker: a sweep archiving a suspicious fraction of
        # managed rules at once is almost certainly a transient empty dir, not
        # a real bulk deletion. Refuse and log loudly rather than wipe. Shared
        # predicate with the sync_rules tool (issue #20) so both guard alike.
        if archive_would_storm(
            len(orphans), len(managed),
            floor=self._archive_circuit_floor,
            fraction=self._archive_circuit_fraction,
        ):
            log(
                f"watcher: REFUSING to archive {len(orphans)} of {len(managed)} "
                f"managed rules in one sweep (circuit breaker: "
                f">{int(self._archive_circuit_fraction * 100)}% and "
                f">{self._archive_circuit_floor}). The rules dir is likely "
                f"transiently empty (failed mount, mid-checkout, or a "
                f"database-authoritative deployment). No rules archived; investigate."
            )
            counts["archive_blocked"] = len(orphans)
        else:
            for row in orphans:
                self._storage.soft_delete_rule(row.id)
                counts["archived"] += 1

        # Build [[slug]] wikilink relations now that all rows are current.
        # Shared with the sync_rules tool so both paths behave identically.
        counts["links"] = build_wikilink_relations(self._storage, self._project_root)
        return counts

    @property
    def cascade_count(self) -> int:
        """Number of actual storage writes the watcher has performed.

        Excludes no-op (content-hash-match) cascades. Used by the
        engine-write-does-not-double-cascade test.
        """
        return self._cascade_count

    # ---- internal: scheduling ----

    def _schedule(self, abs_path: str, op: str) -> None:
        """Debounce: a fresh event for ``abs_path`` resets the timer.

        ``op`` is "upsert" or "delete". Subsequent events for the same
        path overwrite the queued op — the most recent one wins, which
        is the right semantic for an atomic-rename save (modify... then
        delete-the-temp).
        """
        with self._lock:
            existing = self._timers.pop(abs_path, None)
            if existing is not None:
                existing.cancel()
            timer = threading.Timer(
                self._debounce_s, self._fire, args=(abs_path, op),
            )
            timer.daemon = True
            self._timers[abs_path] = timer
            timer.start()

    def _fire(self, abs_path: str, op: str) -> None:
        with self._lock:
            self._timers.pop(abs_path, None)
        try:
            if op == "upsert":
                self._cascade_upsert(Path(abs_path))
            elif op == "delete":
                self._cascade_delete(Path(abs_path))
        except Exception:
            # v1: log-and-continue on any storage error. Phase 4b
            # adds bounded buffer + backoff per docs/watcher-cascade.md.
            pass

    # ---- internal: cascades ----

    def _rel_path(self, path: Path) -> str:
        path = path.resolve()
        try:
            return str(path.relative_to(self._project_root))
        except ValueError:
            return str(path)

    def _is_managed_path(self, file_path: str) -> bool:
        """True when file_path names a file this watcher is responsible for —
        i.e. one under the watched rules_path. A rule whose file_path lives
        elsewhere (a provenance path from import_rules, or an external rule
        dir) is not the watcher's to archive when the file is absent."""
        try:
            abs_p = (self._project_root / file_path).resolve()
        except Exception:
            return False
        return abs_p == self._rules_path or self._rules_path in abs_p.parents

    def _cascade_upsert(self, abs_path: Path) -> str:
        """Mirror a file's content into the storage row. Returns
        'upserted' or 'unchanged'."""
        if not abs_path.exists():
            # The event may have been a transient temp file already
            # gone. Nothing to do.
            return "unchanged"

        parsed = _parse_rule_file(abs_path)
        if parsed is None:
            return "unchanged"

        rel = self._rel_path(abs_path)
        existing = self._storage.find_rule_by_file_path(rel)

        if existing is not None:
            # Content-hash no-op check.
            if existing.content_hash == parsed["content_hash"] and not existing.archived:
                return "unchanged"
            # Update.
            self._storage.update_rule(
                existing.id,
                title=parsed.get("title", existing.title),
                keywords=parsed.get("keywords", existing.keywords),
                description=parsed.get("description", existing.description),
                category=parsed.get("category", existing.category),
                content_hash=parsed["content_hash"],
            )
            if existing.archived:
                # Recreate-after-delete: unarchive (clear archived_at).
                self._storage.restore_rule(existing.id)
            self._cascade_count += 1
            return "upserted"

        # New row.
        self._storage.insert_rule(RuleRow(
            id=0,
            title=parsed.get("title", abs_path.stem),
            keywords=parsed.get("keywords", ""),
            file_path=rel,
            description=parsed.get("description"),
            category=parsed.get("category"),
            content_hash=parsed["content_hash"],
        ))
        self._cascade_count += 1
        return "upserted"

    def _cascade_delete(self, abs_path: Path) -> None:
        # Spurious-delete guard: atomic-rename saves (BSD sed -i, vim's
        # writebackup, several IDE save patterns) fire a trailing
        # FileDeletedEvent for the original path even though the file
        # ends up still present after the rename. If the file is on
        # disk when the debounced delete timer fires, treat the event
        # as a missed upsert and re-cascade the current content.
        if abs_path.exists():
            self._cascade_upsert(abs_path)
            return
        rel = self._rel_path(abs_path)
        existing = self._storage.find_rule_by_file_path(rel)
        if existing is None or existing.archived:
            return
        self._storage.soft_delete_rule(existing.id)
        self._cascade_count += 1
