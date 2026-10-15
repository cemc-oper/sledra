"""Versioned core requirements and explicit future branches for V01--V22."""

from __future__ import annotations

from flow import CUTS

VERSION = "sledra-core-conformance-1"


def py(module, cls, method):
    return f"{module}.{cls}.test_{method}"


def go(package, method):
    return f"go:github.com/cemc-oper/sledra/recorder/{package}:Test{method}"


def core(method):
    return py("test_core", "CoreFlowTests", method)


REQUIREMENTS = {
    "V01": (
        "Synthetic explicit Shell owner while analysis is absent, followed by offline query",
        [
            core("live_shell_owner_records_offline_without_changing_payload"),
            core("frozen_pipeline_resolves_history_without_starting_business"),
        ],
        "takflow launch and real Job/artifact integration require SLD-18/19",
    ),
    "V02": (
        "Local Registry 100 same/distinct launch tokens and immutable identities",
        [
            go("internal/registry", "HundredSameAndDistinctTokens"),
            go("internal/registry", "TwentyIndependentProcessesSameToken"),
        ],
        "cross-node Registry filesystem qualification requires site evidence",
    ),
    "V03": (
        "UTC midnight and business-cycle selection independent from execution date",
        [
            py("test_metrics", "TimingTests", "cross_midnight_exact_nanosecond_and_offset_golden"),
            py(
                "test_metrics", "TimingTests", "cycle_window_uses_business_cycle_not_execution_date"
            ),
        ],
        "two real concurrent engine cycles and delayed callback routing require SLD-18/19",
    ),
    "V04": (
        "Parameterized identity and parent/type validation",
        [
            py(
                "test_projections",
                "ProjectionTests",
                "same_cycle_distinct_run_and_parameterized_instances_never_merge",
            ),
            py(
                "test_projections",
                "ProjectionTests",
                "context_with_valid_hash_but_wrong_parameter_type_stays_unassociated",
            ),
        ],
        "generated takflow mapping requires SLD-17/18",
    ),
    "V05": (
        "Causal retries, forks, explicit selections and finish-scope revision",
        [
            py(
                "test_projections",
                "ProjectionTests",
                "failure_then_success_retains_attempts_and_selects_causal_leaf",
            ),
            py(
                "test_projections",
                "ProjectionTests",
                "fork_requires_selection_and_does_not_choose_latest_timestamp",
            ),
            py(
                "test_projections",
                "ProjectionTests",
                "finish_then_rerun_revises_original_run_and_preserves_old_scope",
            ),
        ],
        "real lifecycle hook qualification requires SLD-18/19",
    ),
    "V06": (
        "Generic synthetic rejection/unknown and late acceptance without resubmission",
        [
            py("test_metrics", "TimingTests", "submission_rejection_duration_is_failure_sample"),
            py("test_metrics", "TimingTests", "unknown_submission_is_censored_never_queue"),
            py(
                "test_metrics",
                "TimingTests",
                "late_acceptance_resolves_submission_unknown_without_resubmission",
            ),
            core("mock_accept_local_commit_failure_and_payload_before_acceptance"),
        ],
        "real scheduler/engine submission callback integration requires SLD-18",
    ),
    "V07": (
        "Missing boundaries, process SIGKILL and source-gap diagnostics",
        [
            py(
                "test_projections",
                "ProjectionTests",
                "end_only_start_only_and_signal_preserve_missing_boundaries",
            ),
            py(
                "test_projections",
                "ProjectionTests",
                "source_gap_and_quarantine_are_visible_even_without_new_business_events",
            ),
            py("sledra_shell", "ShellIntegration", "term_hup_and_kill"),
        ],
        "real node spool loss and shared filesystem outage require site evidence",
    ),
    "V08": (
        "Synthetic engine/control/process/stage states never promote product success",
        [
            py(
                "test_projections",
                "ProjectionTests",
                "engine_complete_job_done_and_process_zero_do_not_confirm_products",
            ),
            py(
                "test_projections",
                "ProjectionTests",
                "stage_failure_is_independent_of_process_exit_zero",
            ),
        ],
        "real MCV abort handler and force-complete callbacks require SLD-19",
    ),
    "V09": (
        "Duplicate/order/late/conflicting versions across raw, projections and reports",
        [
            core("seeded_duplicates_and_source_permutations_rebuild_same_ids"),
            core("partial_tail_quarantine_and_conflicting_event_keep_offsets"),
            py(
                "test_metrics",
                "TimingTests",
                "late_end_publishes_new_report_and_preserves_censored_history",
            ),
        ],
        "real desensitized historical imports require SLD-16",
    ),
    "V10": (
        "Bounded malformed/partial/oversized preservation and continuation",
        [
            py(
                "test_reader",
                "ReaderTests",
                "bad_json_utf8_duplicate_major_and_missing_payload_do_not_block_later_good",
            ),
            py(
                "test_reader",
                "ReaderTests",
                "oversized_line_streams_to_sqlite_with_bounded_python_memory",
            ),
            core("partial_tail_quarantine_and_conflicting_event_keep_offsets"),
        ],
        "target shared-filesystem capacity/performance requires SLD-25",
    ),
    "V11": (
        "Local rename/replacement/truncate and UUID conflict; legacy risk retained",
        [
            py(
                "test_source",
                "SourceIdentityTests",
                "rename_and_hard_link_rotation_share_generation",
            ),
            py(
                "test_source",
                "SourceIdentityTests",
                "truncate_regrow_changes_anchor_even_with_reused_inode_hint",
            ),
            py(
                "test_source",
                "SourceIdentityTests",
                "sealed_uuid_conflict_retains_versions_and_blocks_both_after_restart",
            ),
            py(
                "test_source",
                "SourceIdentityTests",
                "legacy_same_length_unsampled_rewrite_is_explicitly_unproven",
            ),
        ],
        "actual inode allocator reuse/shared-FS behavior remains unverified; arbitrary legacy rewrites are unprovable",
    ),
    "V12": (
        "Raw/checkpoint/queue, derived heads and archive/backup/ack process-death boundaries",
        [
            py(
                "test_transaction",
                "TransactionTests",
                "process_sigkill_at_ingestion_and_ack_cuts_matches_baseline_after_restart",
            ),
            py(
                "test_transaction",
                "TransactionTests",
                "sigkill_during_table_writes_rolls_back_entire_transaction",
            ),
            *[core("crash_" + c.replace(":", "_")) for c in CUTS],
        ],
        "hardware power loss and remote filesystem durability require site evidence",
    ),
    "V13": (
        "Local observation failures preserve explicit-owner stdout/rc/callbacks; storage FULL rolls back",
        [
            py("sledra_shell", "ShellIntegration", "observation_faults_do_not_repeat_payload"),
            py(
                "test_transaction",
                "TransactionTests",
                "sqlite_full_rolls_back_good_fact_and_oversized_isolation",
            ),
            go("spool", "ConcurrentSourcesAndLockConflict"),
        ],
        "real read-only mounts, scheduler callback faults and target full storage require SLD-19/25",
    ),
    "V14": (
        "Offline analysis independence and bounded local spool capacity",
        [
            core("live_shell_owner_records_offline_without_changing_payload"),
            go("spool", "GlobalCapacityStopsNewFilesAndBytes"),
            go("spool", "CapacityOversizeAndUnusableRoot"),
        ],
        "real shared-FS high latency/unreachable paths and approved backlog budgets require SLD-25/A6",
    ),
    "V15": (
        "Explicit-owner Bash options/traps/PIPESTATUS and TERM/HUP/KILL baseline",
        [
            py("sledra_shell", "ShellIntegration", "source_preserves_options_traps_and_pipeline"),
            py("sledra_shell", "ShellIntegration", "term_hup_and_kill"),
            py("sledra_shell", "ShellIntegration", "int_profile_is_not_qualified"),
        ],
        "immediate INT is NOT qualified (baseline Bash profile defers it); real engine callback/other Bash profiles require SLD-19",
    ),
    "V16": (
        "Frozen compatibility, opaque future minor and quarantined major/enum semantics",
        [
            py(
                "test_reader",
                "ValidationTests",
                "major_enum_required_payload_and_authority_fail_without_raw_detail",
            ),
            py(
                "test_reader",
                "ReaderTests",
                "unknown_minor_critical_type_is_persisted_as_opaque_not_success",
            ),
            go("event", "UnknownExtensionAndNoNetwork"),
        ],
        "offline release/upgrade package matrix requires SLD-26",
    ),
    "V17": (
        "Raw product version/rule observations remain distinct and unpromoted",
        [
            py(
                "test_projections",
                "ProjectionTests",
                "product_observations_keep_artifact_version_and_rule_without_promotion",
            )
        ],
        "artifact stability/content/recipient and versioned delivery denominators require SLD-21/22",
    ),
    "V18": (
        "Without a plan, only observed Runs are reported",
        [
            py(
                "test_metrics", "TimingTests", "cycle_window_uses_business_cycle_not_execution_date"
            ),
            py("test_metrics", "TimingTests", "unknown_cycle_is_counted_as_unlocated_with_window"),
        ],
        "ExpectedCycle/maintenance/cancellation and expected-but-unobserved require SLD-21/22",
    ),
    "V19": (
        "Core encoder whitelist and source/recovery symlink rejection",
        [
            go("event", "NegativeCases"),
            py("test_source", "SourceIdentityTests", "paths_and_headers_fail_closed"),
            py(
                "test_transaction",
                "TransactionTests",
                "recovery_and_registry_reject_symlink_escape",
            ),
        ],
        "real producer redaction, full-environment and set-x audit across generated/operational scripts require SLD-19/26",
    ),
    "V20": (
        "Synthetic enabled/disabled owner and observation-error business comparison",
        [
            core("live_shell_owner_records_offline_without_changing_payload"),
            py("sledra_shell", "ShellIntegration", "observation_faults_do_not_repeat_payload"),
        ],
        "generated takflow/resource/dependency equivalence requires SLD-17/18/19; dedicated orvix/data-client adapters excluded",
    ),
    "V21": (
        "Verified receipts then private source deletion, restore/rebuild and stale/corrupt rejection",
        [
            core("acknowledged_source_loss_restore_and_empty_database_rebuild"),
            core("backup_before_all_receipts_is_rejected_without_destination"),
            core("corrupt_archive_blocks_receipt_and_restore"),
        ],
        "expired external evidence rendering, production retention distribution/cleanup and release recovery require SLD-09/26",
    ),
    "V22": (
        "Failure/missing/censored denominators, conditional n and nonadditive parallel intervals",
        [
            py("test_metrics", "TimingTests", "mixed_failure_incomplete_and_censored_golden"),
            py(
                "test_metrics",
                "TimingTests",
                "parallel_intervals_are_not_added_or_called_critical_path",
            ),
            py(
                "test_metrics", "TimingTests", "different_platform_and_config_do_not_mix_statistics"
            ),
        ],
        "product/period delivery report comparisons require SLD-21/22",
    ),
}


def evaluate(cases):
    """Missing, skipped, duplicate or failed mandatory evidence never passes."""
    outcomes = {}
    for case in cases:
        identity = case["test"]
        status = case["status"]
        outcomes[identity] = status if identity not in outcomes else "duplicate"
    rows = []
    for identity, (scope, tests, future) in REQUIREMENTS.items():
        evidence = [{"test": name, "status": outcomes.get(name, "unverified")} for name in tests]
        status = (
            "pass"
            if evidence and all(e["status"] == "pass" for e in evidence)
            else "fail"
            if any(e["status"] in ("fail", "error", "duplicate") for e in evidence)
            else "unverified"
        )
        rows.append(
            {
                "id": identity,
                "status": "fail" if status == "fail" else "unverified",
                "reason": "Core scope is separately gated; full matrix includes pending branches",
                "core": {"status": status, "scope": scope, "evidence": evidence},
                "extension": {"status": "unverified", "reason": future},
            }
        )
    return {
        "version": VERSION,
        "core_pass": all(row["core"]["status"] == "pass" for row in rows),
        "full_matrix_pass": False,
        "rows": rows,
    }
