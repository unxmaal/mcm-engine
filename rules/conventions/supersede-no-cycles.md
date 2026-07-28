# supersede_rule must reject self/cyclic supersede; keep unsupersede_rule as the inverse

## Rule
`supersede_rule(old_id, new_id)` soft-expires `old_id` (status='superseded',
superseded_by=new_id) so it drops out of default search. Two inputs corrupt the
corpus and must be rejected at the tool layer:

- **Self-supersede** (`old_id == new_id`): hides a rule with no successor.
- **Cyclic supersede** (supersede A→B, then B→A): both rules end up superseded
  pointing at each other, so both vanish from search with no live successor.
  Guard = refuse to supersede *by* a rule that is itself `status='superseded'`
  (catches the closing edge of any cycle).

Legitimate chains stay allowed: A→B then B→C is fine (C is live when it supersedes B).

## Recovery
There is an inverse, `unsupersede_rule(rule_id)`: sets status='active', clears
`superseded_by`/`valid_until`, emits an audited `unsuperseded` rule_events row.
Only acts on a currently-`superseded` rule. `restore_rule` only UN-ARCHIVES (it
skips a superseded-but-not-archived rule) — do not conflate the two. Never leave
supersede as a one-way door recoverable only by operator SQL.

## Reference
`tools/rules.py` supersede_rule (guard) + unsupersede_rule; storage
`unsupersede_rule` on both adapters + StorageBackend Protocol. Tests:
`tests/test_supersede_guard.py`. Issues #99 (guard), #100 (recovery).
