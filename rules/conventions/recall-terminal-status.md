# Recalled entries are terminal and invisible (issue #103)

**Keywords:** recall, recall_entry, recalled, terminal status, governance, kb_recall, corpus, invisibility

`recall_entry` (postgres-only, generalizes `kb_recall`) removes a flagged entry
by `(entity_type, id)`:

- **knowledge / negative / error** → hard `DELETE` + a `recall_log` audit row.
- **rule** → NOT deleted. Set `status='recalled'` + a `rule_events` row. A hard
  delete would be resurrected from the rule's file by the next sync.

The terminal `recalled` status must stay invisible and sync-proof. Any new code
path that surfaces or revives a rule MUST honor it in all three enforcement
points, or a recalled (possibly secret-leaking) rule reappears:

1. `tools/search.py` `_score_and_format_rule` — drop `status=='recalled'`
   unconditionally (even with `include_archived`; unlike archived/superseded, a
   recall is not an inspectable soft state).
2. `adapters/*/storage.py` `list_rules` — exclude `recalled` (keeps it out of
   `session_start`'s invariant injection).
3. `files/watcher.py` `_cascade_upsert` — return `unchanged` on a recalled row
   so file-sync never re-mirrors or un-recalls it.

`recall_log` carries an `entity_type` column (default `'knowledge'` for the
pre-#103 knowledge-only rows). `entity_type` is ALWAYS explicit in the tool and
never inferred from the id — knowledge and rule id spaces overlap, and a
mistyped id has destroyed live rules before (the #87/#88 incident).
