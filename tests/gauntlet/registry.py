"""This repo's gauntlet registry, written by `gauntlet init` 0.3.1; edit it by hand from here."""

from pathlib import Path

from gauntlet.registry import Registry

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

# TODO(gauntlet): production: which paths are shipped code; the default is everything outside tests/ and test/.
# TODO(gauntlet): detector_config commit_then_link: helpers that commit besides conn.commit(): {"committers": ("put",)}
# TODO(gauntlet): detector_config stored_pid_liveness: helpers that test a stored pid besides os.kill(pid, 0): {"alive": ("running",)}
# TODO(gauntlet): detector_config unchecked_order: the effects and import-time reads that need an order: {"effects": (...), "reads": (...)}
# TODO(gauntlet): detector_config unpaired_column: the migrations and schema paths: {"migrations": ("app/migrations/",), "schema": (...)}

INDEX = {
    'a-child-that-outlives-its-owner': {'instances': [], 'scanners': [], 'detector': 'orphanable_child'},
    'a-closed-table-fronting-an-open-set': {'instances': [], 'scanners': [], 'detector': 'closed_tables'},
    'a-copy-whose-key-leaves-out-an-input-or-that-nothing-invalidates': {'instances': [], 'scanners': [], 'detector': 'uncached_key'},
    'a-decision-read-back-out-of-a-sentence-the-code-wrote': {'instances': [], 'scanners': [], 'detector': 'prose_match'},
    'a-deploy-runs-two-releases-at-once': {'instances': [], 'scanners': [], 'detector': 'one_sided_schema_check'},
    'a-fact-about-the-harness-recorded-as-a-verdict-on-the-subject': {'instances': [], 'scanners': [], 'detector': 'harness_verdicts'},
    'a-guard-built-and-tested-that-nothing-invokes': {'instances': [], 'scanners': [], 'detector': 'unreached_guards'},
    'a-lifecycle-with-no-declared-state': {'instances': [], 'scanners': [], 'detector': 'unguarded_transition'},
    'a-metric-quoted-without-a-negative-control': {'instances': [], 'scanners': [], 'detector': 'hcl_constant_assert'},
    'a-non-production-run-writes-live-state': {'instances': [], 'scanners': [], 'detector': ['hcl_env_two_inputs', 'hcl_shared_backend']},
    'a-platform-primitive-assumed-universal': {'instances': [{'issue': 197}], 'scanners': []},
    'a-proxy-checked-in-place-of-the-property': {'instances': [], 'scanners': [], 'detector': 'hcl_test_skips_tfvars'},
    'a-shared-store-query-with-no-scope-or-a-scope-with-its-own-idea-of-null': {'instances': [{'issue': 116}], 'scanners': [], 'detector': 'unscoped_query'},
    'a-stored-pid-trusted-as-the-same-process': {'instances': [], 'scanners': [], 'detector': 'stored_pid_liveness'},
    'a-stored-value-with-no-live-writer-or-no-reader': {'instances': [], 'scanners': [], 'detector': 'unpaired_column'},
    'a-wait-with-no-deadline-owner-or-end-state': {'instances': [], 'scanners': [], 'detector': ['undeadlined_wait', 'terminal_without_reader']},
    'an-effect-reachable-from-a-run-that-is-not-the-live-deploy': {'instances': [], 'scanners': [], 'detector': 'effect_reachable'},
    'an-input-accepted-and-silently-ignored': {'instances': [], 'scanners': [], 'detector': 'ignored_options'},
    'an-order-the-code-needs-but-does-not-check': {'instances': [], 'scanners': [], 'detector': ['unchecked_order', 'hcl_order_in_prose']},
    'each-half-verified-against-its-own-spec-the-seam-against-nothing': {'instances': [], 'scanners': [], 'detector': 'env_seams'},
    'external-state-with-no-assertion-on-it': {'instances': [], 'scanners': [], 'detector': ['hcl_lock_missing', 'hcl_unchecked_external']},
    'identity-by-name-not-by-content': {'instances': [], 'scanners': [], 'detector': ['loopback_equality', 'hcl_count_by_position']},
    'irreplaceable-state-with-no-guard-on-its-destruction': {'instances': [], 'scanners': [], 'detector': 'hcl_stateful_unprotected'},
    'nothing-and-failed-return-the-same-value': {'instances': [], 'scanners': [], 'detector': 'empty_on_failure'},
    'one-default-tuned-for-one-member-applied-to-all': {'instances': [], 'scanners': [], 'detector': 'member_defaults'},
    'one-question-two-answers': {'instances': [], 'scanners': [], 'detector': ['duplicated_literals', 'hcl_duplicated_literal']},
    'order-assumed-on-unordered-data': {'instances': [], 'scanners': [], 'detector': 'row_index_keys'},
    'state-is-whichever-row-came-last': {'instances': [], 'scanners': [], 'detector': 'latest_row_state'},
    'two-durable-writes-with-no-repair-for-the-gap': {'instances': [], 'scanners': [], 'detector': ['commit_then_link', 'torn_append']},
}

_CONFORMANCE = "test method in the shipped conformance suite, not a production write path"
_FAIL_OPEN = "hook fails open by design: a hook error must never break the tool call (RULE #61)"

WAIVERS = {
    'a-copy-whose-key-leaves-out-an-input-or-that-nothing-invalidates': {
        'src/mcm_engine/hooks/mcp_enforcement.py:_append_event': 'append-only diagnostic log, one write per line, never read back for a decision',
    },
    'nothing-and-failed-return-the-same-value': {
        'src/mcm_engine/adapters/postgres/storage.py:PostgresStorage.insert_relation:except': "None is the documented 'link already exists' answer on UniqueViolation",
        'src/mcm_engine/hooks/deny_table.py:_dirty_files:except': _FAIL_OPEN,
        'src/mcm_engine/hooks/mcp_enforcement.py:_read_state:except': _FAIL_OPEN,
        'src/mcm_engine/hooks/mcp_enforcement.py:main:except': _FAIL_OPEN,
        'src/mcm_engine/hooks/post_tool_use_failure.py:main:except': _FAIL_OPEN,
        'src/mcm_engine/hooks/pre_compact.py:main:except': _FAIL_OPEN,
        'src/mcm_engine/hooks/session_end.py:main:except': _FAIL_OPEN,
        'src/mcm_engine/hooks/user_prompt_submit.py:main:except': _FAIL_OPEN,
    },
    'state-is-whichever-row-came-last': {
        'src/mcm_engine/adapters/postgres/storage.py:PostgresStorage.find_similar_knowledge': 'rank-ordered FTS match, not a latest-row read',
    },
    'two-durable-writes-with-no-repair-for-the-gap': {
        'src/mcm_engine/testing/conformance.py:CounterConformance.test_increment_last_hit_at_sets_timestamp': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:CounterConformance.test_increment_then_get_returns_value': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:CounterConformance.test_last_flushed_snapshot_equals_live': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:CounterConformance.test_top_by_returns_descending': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:CounterConformance.test_unknown_counter_raises': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:SearchConformance.test_search_pinned_flag_propagates': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:StorageConformance.test_entry_insert_find_update_delete': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:StorageConformance.test_get_last_snapshot': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:StorageConformance.test_insert_and_list_rule_events': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:StorageConformance.test_insert_relation_and_list_outgoing_incoming': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:StorageConformance.test_list_archived_rules': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:StorageConformance.test_list_rule_events_respects_limit': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:StorageConformance.test_rule_event_actor_defaults_to_nobody': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:StorageConformance.test_set_pinned_false_restores': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:StorageConformance.test_set_pinned_then_list_pinned': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:StorageConformance.test_snapshot_seq_increments_per_session': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:StorageConformance.test_soft_delete_and_restore_rule': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:StorageConformance.test_transaction_commits_on_clean_exit': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:StorageConformance.test_transaction_rolls_back_on_exception': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:StorageConformance.test_update_knowledge': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:StorageConformance.test_update_rule': _CONFORMANCE,
        'src/mcm_engine/testing/conformance.py:WriteThroughCounterConformance.test_increments_are_mirrored_into_entries': _CONFORMANCE,
    },
}
for _cid, _sites in WAIVERS.items():
    INDEX[_cid]["waivers"] = _sites

# Unbound hits seeded by init; lower it as the backlog is bound, never raise it.
BACKLOG_PIN = 103

DETECTOR_CONFIG = {
    "effect_reachable": {"effects": ("mcp_http_call_tool", "git_commit_tree", "_http_chat")},
    "empty_on_failure": {"choosers": ("build_verified_context", "open_storage")},
    "one_sided_schema_check": {"versions": ("CORE_VERSION", "CONTRACT_VERSION")},
    "prose_match": {"fields": ("summary", "detail"), "migrations": ("src/mcm_engine/sanitize.py",)},
    "unscoped_query": {"columns": ("principal", "workspace")},
}

MOVE_WAIVERS = {
}

REGISTRY = Registry(
    root=ROOT,
    index=INDEX,
    snapshot_file=HERE / "skill_classes.json",
    defects_file=HERE / "defects.json",
    backlog_file=HERE / "backlog.json",
    backlog_pin=BACKLOG_PIN,
    frontends=('python', 'hcl'),
    move_waivers=MOVE_WAIVERS,
    detector_config=DETECTOR_CONFIG,
    repo_slug="unxmaal/mcm-engine",
    defect_label="bug",
)
