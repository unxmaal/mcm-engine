"""This repo's gauntlet registry, written by `gauntlet init` 0.3.0; edit it by hand from here."""

from pathlib import Path

from gauntlet.registry import Registry

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent

# TODO(gauntlet): production: which paths are shipped code; the default is everything outside tests/ and test/.
# TODO(gauntlet): repo_slug: "owner/repo" of the issue tracker, for `gauntlet snapshot-defects` and `gauntlet audit`.
# TODO(gauntlet): detector_config commit_then_link: helpers that commit besides conn.commit(): {"committers": ("put",)}
# TODO(gauntlet): detector_config effect_reachable: the effects and the live-run predicate: {"effects": ("send",), "live": ("LIVE_HOME",)}
# TODO(gauntlet): detector_config empty_on_failure: the functions that choose a store: {"choosers": ("backend",)}
# TODO(gauntlet): detector_config one_sided_schema_check: the schema version names and check entry points: {"versions": (...), "phases": (...)}
# TODO(gauntlet): detector_config prose_match: the fields that hold prose: {"fields": ("note", "reason")}
# TODO(gauntlet): detector_config stored_pid_liveness: helpers that test a stored pid besides os.kill(pid, 0): {"alive": ("running",)}
# TODO(gauntlet): detector_config unchecked_order: the effects and import-time reads that need an order: {"effects": (...), "reads": (...)}
# TODO(gauntlet): detector_config unpaired_column: the migrations and schema paths: {"migrations": ("app/migrations/",), "schema": (...)}
# TODO(gauntlet): detector_config unscoped_query: the scope columns and tables of a shared store: {"columns": ("tenant_id",), "tables": ("jobs",)}

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
    'a-proxy-checked-in-place-of-the-property': {'instances': [], 'scanners': [], 'detector': 'hcl_test_skips_tfvars'},
    'a-shared-store-query-with-no-scope-or-a-scope-with-its-own-idea-of-null': {'instances': [], 'scanners': [], 'detector': 'unscoped_query'},
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

# Unbound hits seeded by init; lower it as the backlog is bound, never raise it.
BACKLOG_PIN = 105

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
)
