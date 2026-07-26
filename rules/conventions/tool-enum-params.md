# Closed-set MCP tool params must be Literals, not bare `str`

## Convention
When an `@mcp.tool()` parameter has a known, finite vocabulary, annotate it with a
`typing.Literal` (or `Literal | None` when there is a skip/omit case), never a
bare `str` with the allowed values only in the docstring. FastMCP compiles the
Literal into a JSON-schema `enum`, so the contract reaches the model at the call
site instead of living in prose. This is the "expressive schema over prose"
principle from the c5 context-engineering work.

## How to keep it drift-free
Derive the Literal next to its source of truth and guard it:
- Entity kinds: `EntityTypeLiteral` in `backends/__init__.py`, guarded against `EntityType`.
- Rule hierarchy: `ScopeLiteral` / `KindLiteral` in `hierarchy.py`, guarded against `SCOPES` / `KINDS`.
- Relations: `RelationType` in `tools/relations.py`, with `VALID_RELATIONS = set(get_args(...))`.
- Search scope: `SearchScope` in `tools/search.py`, guarded against `_SCOPE_MAP` keys (note: search labels are plural `errors`/`rules`, distinct from EntityType's singular values).

Keep the runtime validation too (belt-and-suspenders for non-schema callers).

## Watch-outs
- Params that use a sentinel to mean "skip" (e.g. `set_rule_metadata.importance = -1`)
  can't be naively constrained; use `Literal | None = None` for the string axes and
  leave sentinel ints alone unless you redesign the skip mechanism.
- `list[dict]` params (`import_rules.rules`, `sift_candidates.candidates`) need a
  typed item model, not an enum swap.

Regression coverage: `tests/test_tool_enum_params.py`, `tests/test_relation_enum.py`.
Issue: c5_modernization Phase 3.
