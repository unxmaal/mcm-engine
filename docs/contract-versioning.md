# Adapter contract versioning

The four (eventually five) `Protocol` classes in `mcm_engine/backends/__init__.py`
— `StorageBackend`, `CounterStore`, `SearchBackend`, `SessionStore` — are the
public contract third-party adapters implement. This document defines how that
contract is versioned, what counts as a breaking change, and how adapters
declare compatibility.

## The version constant

A single module-level integer:

```python
# mcm_engine/backends/__init__.py
CONTRACT_VERSION = 1
```

This integer is bumped **only** when a change to the four Protocol classes (or
the dataclasses they exchange — `SearchHit`, `CounterSnapshot`, etc.) would
cause an existing adapter compiled against the previous version to malfunction.

We do not use semver. We use a single integer because it cannot be
misinterpreted. `2 > 1`; no ambiguity about whether a change was "minor enough."
When the number of third-party adapters grows large enough that the bump
cost is felt by adapter authors, we will revisit. Until then: simpler is
better.

## What is a breaking change

The rule: **a change is breaking if an adapter that passed conformance against
the previous version could fail conformance — or worse, silently misbehave —
against the new version.**

The following are breaking and require a bump:

- Adding a new method (or property) to a Protocol class. Existing adapters
  do not implement it; the engine will raise `AttributeError` when it calls.
- Removing a method.
- Renaming a method, parameter, or dataclass field.
- Changing a method's signature: adding a required parameter, removing a
  parameter, narrowing a parameter's accepted type, widening a return type.
- Changing the *semantics* of an existing method without changing its
  signature. Examples that count as semantic changes:
  - "This method must now be idempotent."
  - "This method must now return results in ascending order."
  - "This method may now raise `LockTimeout`."
  - "`scope` now defaults to the caller's identity instead of global."
- Adding a required field to a dataclass passed between engine and adapter
  (`SearchHit`, `CounterSnapshot`).
- Changing a dataclass field's type.
- Changing the contract for what an existing exception means.

The following are **not** breaking and do **not** require a bump:

- Adding a new optional dataclass field with a default value. Existing
  adapters constructing the dataclass without the new field continue to work.
- Adding new exception types the engine catches but does not require adapters
  to raise.
- Adding new entry-point groups (e.g. `mcm_engine.adapters.embeddings` when
  vector search lands) — adapters in the existing groups are unaffected.
- Adding a new optional capability flag (see "Capabilities," below) that
  adapters opt into.
- Internal refactors that do not change any externally visible Protocol shape.
- Documentation, type-hint tightening that does not narrow the runtime
  contract, performance improvements.

When in doubt: bump. The cost of a false-positive bump is one extra `int`
change in adapter `pyproject.toml` files. The cost of a false-negative —
shipping a breaking change without a bump — is a third-party adapter that
fails in confusing ways and erodes trust in the contract.

## Capabilities — the escape hatch for non-breaking additions

When the engine wants to call a new method without making it a hard
requirement of the contract, the new method is registered as a
**capability**:

```python
class Capability(StrEnum):
    BULK_INSERT = "bulk_insert"
    VECTOR_SEARCH = "vector_search"
    # ...

class StorageBackend(Protocol):
    capabilities: ClassVar[set[Capability]]

    # always-required methods
    def find_knowledge(self, ...): ...

    # capability-gated method — only called if BULK_INSERT in capabilities
    def insert_many(self, ...): ...
```

Adapters declare which capabilities they support via the class-level
`capabilities` set. The engine checks before calling any
capability-gated method:

```python
if Capability.BULK_INSERT in storage.capabilities:
    storage.insert_many(rows)
else:
    for row in rows:
        storage.insert_one(row)
```

Adding a new capability + capability-gated method is **not** a breaking
change. Adapters that don't know about it simply don't opt in; the engine
takes the fallback path.

This is the mechanism for evolving the contract without a bump. Most additions
should go through capabilities. Bumping `CONTRACT_VERSION` is reserved for
changes that truly affect the always-required surface.

## How adapters declare compatibility

Every adapter class declares the version it was built against:

```python
class PostgresStorage:
    CONTRACT_VERSION = 1
    capabilities = {Capability.BULK_INSERT}

    def find_knowledge(self, ...): ...
    def insert_many(self, ...): ...
```

The engine's adapter registry checks this at registration time (not at first
call — fail fast at boot, not in production traffic):

```python
def register_adapter(group, name, cls):
    if cls.CONTRACT_VERSION != mcm_engine.backends.CONTRACT_VERSION:
        raise ContractVersionError(
            f"Adapter {name} declares CONTRACT_VERSION={cls.CONTRACT_VERSION}, "
            f"engine requires {mcm_engine.backends.CONTRACT_VERSION}. "
            f"Either upgrade {name} to the new contract or pin a compatible engine."
        )
```

The error message names the engine version, the adapter version, and the
adapter — enough to act on without needing to read the engine source.

## Bump checklist

When you intend to bump `CONTRACT_VERSION`:

1. **Confirm the change is actually breaking.** Re-read "What is a breaking
   change" above. If you can route it through a new capability, do that
   instead.
2. **Bump the integer in one place** — `mcm_engine/backends/__init__.py`.
3. **Update the changelog** below in this document, with a one-line summary
   of what changed and which methods/dataclasses are affected.
4. **Update first-party reference adapters** — they all need their
   `CONTRACT_VERSION` integer bumped and any code changes the new contract
   demands. The conformance suite must pass on each.
5. **Update the conformance suite** to exercise the new shape. If the
   conformance suite passes against the previous adapter shape and the new
   one, the bump's reason wasn't real.
6. **Note the bump in the engine's release notes** so third-party adapter
   authors see it.

## Version log

| Version | Date       | Change |
|---------|------------|--------|
| 1       | Phase 0    | Initial contract: StorageBackend, CounterStore, SearchBackend, SessionStore. |
