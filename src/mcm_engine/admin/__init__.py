"""KB admin tuning plane (issue #64, Phase 3).

A small co-located service that renders the rules as an editable grid and lets
an admin tune the hierarchy axes (importance / scope / kind / category) with
realtime colorize on change.

Architecture (recorded on #64): the admin plane sits BESIDE the MCP, both on
the shared ``mcm_engine`` storage library — above the MCP's agent-facing policy
(nudges, blast-radius guards) but on top of the storage layer's integrity.
Reads go direct (``storage.list_rules``); writes go through
``storage.set_rule_metadata`` so every change is validated and emits the
audited ``rule_events`` row the colorize view reads. It never re-implements SQL.
"""
