"""V07/V22: UTC/monotonic pairing, incomplete denominators and immutable reports."""

from __future__ import annotations

import copy
import itertools
import json
import random
import signal
import sqlite3
import subprocess
import sys
import unittest
from unittest.mock import patch

import test_projections as projections
from test_store import LATER, record
from test_source import segment

from sledra_analysis.ingest.transaction import AtomicIngestor
from sledra_analysis.metrics.clock import LIMIT, utc_ns, wall_interval
from sledra_analysis.metrics.engine import MetricsEngine
from sledra_analysis.metrics.reducer import pair, reconstruct_timing, summarize
from sledra_analysis.projections.reducer import event_ref, reconstruct
from sledra_analysis.projections.engine import ProjectionError
from sledra_analysis.snapshot import read_inputs
from sledra_analysis.store.canonical import canonical_bytes, canonical_hash
from sledra_analysis.store.raw import RawStore
from sledra_analysis.store.recovery import RecoveryStore

QUALITY = {"basis": "producer_clock", "precision_ns": 0, "clock_error_bound_ns": 0}
AS_OF = "2026-10-11T00:00:00Z"
PROPERTY_OBSERVATIONS = []


class TimingTests(unittest.TestCase):
    event = projections.ProjectionTests.event
    ingest = projections.ProjectionTests.ingest
    context_for = projections.ProjectionTests.context_for
    register = projections.ProjectionTests.register

    def setUp(self):
        projections.ProjectionTests.setUp(self)
        self.metrics = MetricsEngine(self.store)

    def boundary(self, name, payload, stamp="2026-10-10T23:59:58Z", **kwargs):
        return self.event(
            name, payload, occurred_at=stamp, time_quality=copy.deepcopy(QUALITY), **kwargs
        )

    def process(
        self, *, start="2026-10-10T23:59:58Z", end="2026-10-11T00:00:00Z", code=0, context=None
    ):
        c = context or self.context
        return [
            self.register(c),
            self.boundary(
                "attempt.started",
                {"invocation_id": c["attempt_id"], "process_role": c["process_role"]},
                start,
                context=c,
            ),
            self.boundary(
                "attempt.exited",
                {"termination": "exited", "exit_code": code, "process_role": c["process_role"]},
                end,
                context=c,
            ),
        ]

    def stages(
        self,
        interval="a",
        *,
        start="2026-10-10T23:59:58Z",
        end="2026-10-11T00:00:00Z",
        result="success",
        duration=None,
    ):
        p = {"stage_key": "forecast", "interval_id": interval}
        return [
            self.boundary("stage.started", p, start),
            self.boundary(
                "stage.ended",
                dict(
                    p, result=result, **({"duration_ns": duration} if duration is not None else {})
                ),
                end,
            ),
        ]

    def publish(self, **kwargs):
        return self.metrics.publish(report_as_of=AS_OF, published_at=LATER, **kwargs)

    def rows(self, metric, report=None):
        return [
            r for r in (report or self.metrics.report()).data["intervals"] if r["metric"] == metric
        ]

    def stats(self, metric, report=None):
        return [
            r
            for r in (report or self.metrics.report()).data["statistics"]
            if r["stratum"]["metric"] == metric
        ]

    def test_cross_midnight_exact_nanosecond_and_offset_golden(self):
        self.ingest(
            self.process(
                start="2026-10-10T23:59:59.999999999Z", end="2026-10-11T08:00:00.000000001+08:00"
            )
        )
        self.publish()
        row = self.rows("process_execution")[0]
        # End is 1ns after as_of, so its duration is displayed but excluded from means.
        self.assertEqual(row["duration_ns"], 2)
        self.assertFalse(row["statistics_eligible"])
        self.assertIn("after_report_as_of", row["flags"])
        report = self.metrics.publish(report_as_of="2026-10-11T00:00:01Z", published_at=LATER)
        self.assertEqual(self.rows("process_execution", report)[0]["duration_ns"], 2)
        self.assertEqual(utc_ns("2026-10-11T08:00:00+08:00"), utc_ns(AS_OF))

    def test_unknown_clock_is_not_assumed_synchronized(self):
        events = self.process()
        events[1]["time_quality"] = {"basis": "producer_clock"}
        self.ingest(events)
        self.publish()
        row = self.rows("process_execution")[0]
        self.assertEqual(row["clock"]["nominal_ns"], 2_000_000_000)
        self.assertEqual(row["clock"]["quality"], "uncertain")
        self.assertIsNone(row["duration_ns"])

    def test_bounded_cross_host_clock_range(self):
        events = self.stages()
        for e in events:
            e["time_quality"]["clock_error_bound_ns"] = 100
        events[1]["source"]["source_id"] = "other-host"
        self.ingest(events)
        self.publish()
        row = self.rows("stage")[0]
        self.assertEqual(
            (row["clock"]["lower_ns"], row["clock"]["upper_ns"]), (1_999_999_800, 2_000_000_200)
        )
        self.assertEqual(row["clock"]["quality"], "bounded")
        self.assertIsNone(row["duration_ns"])

    def test_offset_bound_larger_than_delta_is_uncertain(self):
        events = self.stages()
        for e in events:
            e["time_quality"]["clock_error_bound_ns"] = 3_000_000_000
        self.ingest(events)
        self.publish()
        clock = self.rows("stage")[0]["clock"]
        self.assertEqual(clock["quality"], "uncertain")
        self.assertEqual(clock["lower_ns"], 0)

    def test_negative_wall_duration_is_flagged(self):
        self.ingest(self.stages(start=AS_OF, end="2026-10-10T23:59:58Z"))
        self.publish()
        row = self.rows("stage")[0]
        self.assertEqual(row["clock"]["quality"], "conflict")
        self.assertIsNone(row["duration_ns"])
        self.assertIn("wall_clock_conflict", row["flags"])

    def test_unknown_native_timezone_remains_raw_and_unavailable(self):
        events = self.stages(start="2026-10-10T23:59:58-00:00", duration=77)
        self.ingest(events)
        report = self.publish()
        row = self.rows("stage")[0]
        self.assertEqual(row["clock"]["quality"], "unavailable")
        self.assertEqual(row["duration_ns"], 77)
        self.assertIn("endpoint_time_unavailable", row["flags"])
        self.assertFalse(row["statistics_eligible"])
        self.assertEqual(self.stats("stage")[0]["statistics"]["n"], 0)
        self.assertEqual(self.stats("stage")[0]["statistics"]["excluded"], 1)
        self.assertEqual(len(report.data["diagnostics"]), 0)

    def mono(self, events, delta=123):
        for index, e in enumerate(events):
            e["source"]["boot_id"] = "boot-a"
            e["source"]["seq"] = index
            e["extensions"] = {
                "sledra.timing": {
                    "version": "1",
                    "clock_id": "CLOCK_MONOTONIC",
                    "monotonic_ns": 1000 + index * delta,
                }
            }

    def test_monotonic_preferred_during_wall_clock_jump(self):
        events = self.process(start=AS_OF, end="2026-10-10T23:59:58Z")
        self.mono(events[1:])
        self.ingest(events)
        self.publish()
        row = self.rows("process_execution")[0]
        self.assertEqual(row["duration_ns"], 123)
        self.assertEqual(row["duration_basis"], "monotonic")
        self.assertEqual(row["clock"]["quality"], "conflict")

    def test_monotonic_domain_and_version_rejected(self):
        for mutation in ("boot", "session", "clock", "version", "negative", "boolean"):
            events = self.stages(mutation)
            self.mono(events)
            if mutation == "boot":
                events[1]["source"]["boot_id"] = "boot-b"
            if mutation == "session":
                events[1]["source"]["session_id"] = "session-b"
            if mutation == "clock":
                events[1]["extensions"]["sledra.timing"]["clock_id"] = "other"
            if mutation == "version":
                events[1]["extensions"]["sledra.timing"]["version"] = "2"
            if mutation == "negative":
                events[1]["extensions"]["sledra.timing"]["monotonic_ns"] = 0
            if mutation == "boolean":
                events[1]["extensions"]["sledra.timing"]["monotonic_ns"] = True
            self.ingest(events)
        self.publish()
        self.assertTrue(
            all(r["pairing"] == "conflict" and r["duration_ns"] is None for r in self.rows("stage"))
        )

    def test_declared_stage_duration_and_conflicting_measurement(self):
        events = self.stages("declared", duration=77)
        events[0]["time_quality"] = {"basis": "unknown", "reason": "synthetic"}
        self.ingest(events)
        other = self.stages("conflict", duration=99)
        self.mono(other)
        self.ingest(other)
        self.publish()
        by_key = {r["key"][-1]: r for r in self.rows("stage")}
        self.assertEqual(by_key["declared"]["duration_ns"], 77)
        self.assertEqual(by_key["declared"]["duration_basis"], "producer_duration")
        self.assertEqual(by_key["conflict"]["pairing"], "conflict")

    def test_duration_evidence_and_cutoff_qualification_are_separate(self):
        cases = (
            ("declared_before", "declared", "2026-10-10T23:59:59Z", True),
            ("declared_equal", "declared", AS_OF, True),
            ("declared_after", "declared", "2026-10-11T00:00:01Z", False),
            ("declared_unknown_start", "declared", AS_OF, False),
            ("declared_unknown_end", "declared", "2026-10-11T00:00:00-00:00", False),
            ("monotonic_unknown_end", "monotonic", "2026-10-11T00:00:00-00:00", False),
            ("wall_equal", "wall", AS_OF, True),
        )
        rows = []
        for label, basis, end_at, eligible in cases:
            with self.subTest(label=label):
                declared = {
                    "declared_before": 1_000_000_000,
                    "declared_equal": 2_000_000_000,
                    "declared_after": 3_000_000_000,
                }.get(label, 7)
                events = self.stages(
                    label, end=end_at, duration=declared if basis == "declared" else None
                )
                if label == "declared_equal":
                    events[1]["payload"]["result"] = "failure"
                if label == "declared_unknown_start":
                    events[0]["occurred_at"] = None
                    events[0]["time_quality"] = {"basis": "unknown", "reason": "missing"}
                if basis == "monotonic":
                    self.mono(events)
                row = pair(
                    "stage",
                    [self.run["run_id"], self.context["attempt_id"], label],
                    events[:1],
                    events[1:],
                    report_as_of=AS_OF,
                    refs=[],
                )
                row["stratum"] = {"metric": "stage"}
                rows.append(row)
                self.assertEqual(row["pairing"], "paired")
                self.assertIsNotNone(row["duration_ns"])
                self.assertEqual(row["statistics_eligible"], eligible)
                if not eligible:
                    self.assertTrue(
                        set(row["flags"]) & {"after_report_as_of", "endpoint_time_unavailable"}
                    )
        stat = summarize(rows)[0]
        self.assertEqual(stat["total"], len(cases))
        self.assertEqual(stat["statistics"]["n"], 3)
        self.assertEqual(stat["statistics"]["excluded"], 4)
        self.assertEqual(stat["statistics"]["failure_n"], 1)
        self.assertEqual(stat["statistics"]["p95_ns"], 2_000_000_000)
        self.assertEqual(
            stat["statistics"]["duration_basis_counts"],
            {"producer_duration": 2, "utc_declared_bounds": 1},
        )

    def test_end_only_never_manufactures_duration_or_start(self):
        self.ingest(self.stages(duration=100)[1:])
        self.publish()
        row = self.rows("stage")[0]
        self.assertEqual(row["pairing"], "end_only")
        self.assertIsNone(row["start_at"])
        self.assertIsNone(row["duration_ns"])
        self.assertEqual(row["unpaired_declared_duration_ns"], 100)

    def test_start_only_keeps_censor_and_observed_lower_bound(self):
        self.ingest(self.stages()[:1])
        self.publish()
        row = self.rows("stage")[0]
        self.assertTrue(row["right_censored"])
        self.assertEqual(row["observed_lower_bound_ns"], 2_000_000_000)
        self.assertIsNone(row["duration_ns"])

    def test_missing_stage_is_unavailable_not_whole_job_duration(self):
        self.ingest(self.process())
        self.publish()
        self.assertEqual(self.rows("stage")[0]["pairing"], "unavailable")
        self.assertIsNone(self.rows("stage")[0]["duration_ns"])

    def test_missing_both_process_endpoints_is_in_denominator(self):
        self.ingest([self.register()])
        self.publish()
        stat = self.stats("process_execution")[0]
        self.assertEqual((stat["total"], stat["unavailable"], stat["statistics"]["n"]), (1, 1, 0))

    def test_raw_invalid_association_keeps_bytes_and_other_timing_samples(self):
        valid = self.stages("valid", duration=10)
        invalid = self.boundary(
            "stage.started", {"stage_key": "forecast", "interval_id": "invalid"}, assoc={}
        )
        original = canonical_bytes(invalid) + b"\n"
        self.store.ingest([record(invalid, locator="invalid.jsonl", raw=original)])
        self.ingest(valid)
        state = self.engine.rebuild(published_at=LATER)
        report = self.publish()
        self.assertEqual(self.rows("stage", report)[0]["duration_ns"], 10)
        self.assertTrue(
            any(
                d.get("fact") == event_ref({"event": invalid, "hash": canonical_hash(invalid)})
                for d in report.data["diagnostics"]
            )
        )
        self.assertEqual(self.store.sources(invalid["event_id"])[0].raw_bytes, original)
        self.assertIsNotNone(state)

    def test_atomic_ingestor_quarantines_invalid_association(self):
        invalid = self.boundary(
            "stage.started", {"stage_key": "forecast", "interval_id": "invalid"}, assoc={}
        )
        shared = self.root / "shared"
        shared.mkdir()
        raw = segment(canonical_bytes(invalid) + b"\n")
        (shared / "invalid.sealed.jsonl").write_bytes(raw)
        AtomicIngestor(self.store, shared, "root").scan("invalid.sealed.jsonl", collected_at=LATER)
        report = self.publish()
        self.assertEqual(len(self.store.quarantines()), 1)
        self.assertEqual(self.stats("stage", report)[0]["statistics"]["n"], 0)
        self.assertFalse(
            any(invalid["event_id"] in str(row["because"]) for row in self.rows("stage", report))
        )

    def test_mixed_failure_incomplete_and_censored_golden(self):
        events = (
            self.stages("success", duration=10)
            + self.stages("failure", result="failure", duration=30)
            + self.stages("end-only")[1:]
            + self.stages("start-only")[:1]
        )
        self.ingest(events)
        self.publish()
        stat = self.stats("stage")[0]
        self.assertEqual(
            (stat["total"], stat["missing_start"], stat["missing_end"], stat["right_censored"]),
            (4, 1, 1, 1),
        )
        self.assertEqual(stat["outcome_counts"], {"failure": 1, "success": 2, "unknown": 1})
        self.assertEqual(stat["statistics"]["n"], 2)
        self.assertEqual(stat["statistics"]["excluded"], 2)
        self.assertEqual(stat["statistics"]["failure_n"], 1)
        self.assertEqual(stat["statistics"]["mean_ns"], {"numerator": "20", "denominator": "1"})
        self.assertEqual(stat["statistics"]["median_ns"], {"numerator": "20", "denominator": "1"})
        self.assertEqual(stat["statistics"]["p95_ns"], 30)

    def test_parallel_intervals_are_not_added_or_called_critical_path(self):
        self.ingest(self.stages("parallel-a") + self.stages("parallel-b"))
        report = self.publish()
        self.assertEqual(len(self.rows("stage")), 2)
        self.assertIsNone(report.data["run_elapsed_ns"])
        self.assertEqual(report.data["critical_path"]["state"], "unavailable")
        self.assertEqual(self.stats("stage")[0]["statistics"]["n"], 2)

    def test_wait_id_and_selector_require_exact_match(self):
        events = [
            self.boundary("data.wait_started", {"wait_id": "wait-a", "selector": "input-a"}),
            self.boundary(
                "data.wait_ended",
                {"wait_id": "wait-b", "selector": "input-a", "result": "ready"},
                AS_OF,
            ),
        ]
        self.ingest(events)
        self.publish()
        self.assertEqual(
            {r["pairing"] for r in self.rows("dependency_wait")}, {"start_only", "end_only"}
        )

    def submission(self, outcome):
        events = [
            self.boundary(
                "submission.requested",
                {
                    "submission_instance": "submit-a",
                    "platform_id": "wsl2",
                    "scheduler": "synthetic-scheduler",
                    "resource_ref": "synthetic",
                },
            )
        ]
        if outcome == "accepted":
            job = self.bundle["job_ref"]
            self.engine.import_documents([projections.docrecord(job)])
            events.append(
                self.boundary(
                    "submission.accepted",
                    {
                        "submission_instance": "submit-a",
                        "job_ref_id": job["job_ref_id"],
                        "confirmation_basis": "synthetic",
                    },
                    AS_OF,
                    assoc={
                        "run_id": self.run["run_id"],
                        "attempt_id": self.context["attempt_id"],
                        "task_instance_id": self.context["task_instance_id"],
                        "job_ref_id": job["job_ref_id"],
                    },
                )
            )
        else:
            events.append(
                self.boundary(
                    "submission.failed",
                    {
                        "submission_instance": "submit-a",
                        "outcome": outcome,
                        "failure_stage": "request",
                        "error_class": "synthetic",
                    },
                    AS_OF,
                )
            )
        return events

    def test_submission_rejection_duration_is_failure_sample(self):
        self.ingest(self.submission("not_accepted"))
        self.publish()
        self.assertEqual(self.rows("submission_processing")[0]["duration_ns"], 2_000_000_000)
        self.assertEqual(self.stats("submission_processing")[0]["statistics"]["failure_n"], 1)

    def test_unknown_submission_is_censored_never_queue(self):
        self.ingest(self.submission("unknown"))
        report = self.publish()
        row = self.rows("submission_processing")[0]
        self.assertEqual(row["pairing"], "start_only")
        self.assertTrue(row["right_censored"])
        self.assertIsNone(row["duration_ns"])
        self.assertTrue(
            any(d["reason"] == "submission_outcome_unknown" for d in report.data["diagnostics"])
        )
        self.assertFalse(self.rows("scheduler_accounting_queue"))

    def job_observation(self, state, time):
        j = self.bundle["job_ref"]
        return self.boundary(
            "job.state_observed",
            {
                "job_ref_id": j["job_ref_id"],
                "raw_state": state.upper(),
                "normalized_state": state,
                "observation_time": time,
            },
            time,
            assoc={"run_id": j["run_id"], "job_ref_id": j["job_ref_id"]},
        )

    def test_polling_only_brackets_transition_not_exact_queue(self):
        self.engine.import_documents([projections.docrecord(self.bundle["job_ref"])])
        self.ingest(
            [
                self.job_observation("queued", "2026-10-10T23:59:58Z"),
                self.job_observation("running", AS_OF),
            ]
        )
        self.publish()
        row = self.rows("scheduler_sampled_transition")[0]
        self.assertEqual(row["pairing"], "paired")
        self.assertIsNone(row["duration_ns"])
        self.assertEqual(row["transition_window"]["no_later_than"], AS_OF)
        self.assertFalse(self.rows("scheduler_accounting_queue"))

    def accounting(self):
        self.engine.import_documents([projections.docrecord(self.bundle["job_ref"])])
        event = self.job_observation("running", AS_OF)
        self.ingest([event])
        return {
            "version": "1",
            "adapter_version": "synthetic-accounting-1",
            "run_id": self.run["run_id"],
            "job_ref_id": self.bundle["job_ref"]["job_ref_id"],
            "submission_instance": "submit-a",
            "interval_id": "queue-a",
            "queue_basis": "eligible",
            "includes_hold": False,
            "start": {"occurred_at": "2026-10-10T23:59:58Z", "time_quality": QUALITY},
            "end": {"occurred_at": AS_OF, "time_quality": QUALITY},
            "because": [event["event_id"] + "@" + canonical_hash(event)],
        }

    def test_accounting_requires_evidence_generation_and_hold_semantics(self):
        record = self.accounting()
        self.publish(accounting=[record])
        row = self.rows("scheduler_accounting_queue")[0]
        self.assertEqual(row["duration_ns"], 2_000_000_000)
        self.assertEqual(row["stratum"]["queue_basis"], "eligible")
        self.assertFalse(row["includes_hold"])
        self.assertEqual(
            self.metrics.inputs(self.metrics.report().report_id)["accounting"], [record]
        )
        bad = copy.deepcopy(record)
        bad["submission_instance"] = "reused-job-id-wrong-generation"
        with self.assertRaises(ValueError):
            self.publish(accounting=[bad])
        bad = copy.deepcopy(record)
        bad["because"] = ["missing-fact"]
        with self.assertRaises(ProjectionError):
            self.publish(accounting=[bad])
        bad = copy.deepcopy(record)
        del bad["includes_hold"]
        with self.assertRaises(ValueError):
            self.publish(accounting=[bad])

    def test_conflicting_accounting_interval_rejected_atomically(self):
        record = self.accounting()
        good = self.publish(accounting=[record])
        bad = copy.deepcopy(record)
        bad["includes_hold"] = True
        with self.assertRaises(ProjectionError):
            self.publish(accounting=[record, bad])
        self.assertEqual(self.metrics.report(), good)

    def test_engine_observed_interval_has_versioned_explicit_mapping(self):
        def observe(raw, time):
            return self.boundary(
                "engine.state_observed",
                {
                    "engine_instance": "engine-a",
                    "node_path": "/forecast",
                    "raw_state": raw,
                    "observation_time": time,
                    "source_position": raw,
                },
                time,
            )

        self.ingest([observe("submitted", "2026-10-10T23:59:58Z"), observe("active", AS_OF)])
        self.publish()
        self.assertFalse(self.rows("engine_observed_interval"))
        mapping = {
            "version": "synthetic-engine-1",
            "submitted": ["submitted"],
            "active": ["active"],
        }
        self.publish(engine_mapping=mapping)
        row = self.rows("engine_observed_interval")[0]
        self.assertEqual(row["duration_ns"], 2_000_000_000)
        self.assertEqual(row["stratum"]["engine_mapping_version"], "synthetic-engine-1")
        self.assertFalse(self.rows("scheduler_accounting_queue"))

    def test_late_end_publishes_new_report_and_preserves_censored_history(self):
        events = self.stages()
        self.ingest(events[:1])
        old = self.publish()
        self.ingest(events[1:])
        self.assertEqual(self.metrics.status()["state"], "pending")
        new = self.publish()
        self.assertNotEqual(old.report_id, new.report_id)
        self.assertEqual(self.metrics.report(old.report_id), old)
        self.assertEqual(self.rows("stage", new)[0]["duration_ns"], 2_000_000_000)
        self.assertEqual(self.metrics.status()["state"], "current")

    def test_duplicate_publication_keeps_original_report_time(self):
        events = self.stages()
        self.ingest(events)
        old = self.publish()
        self.ingest(events)
        new = self.metrics.publish(report_as_of=AS_OF, published_at="2026-10-12T00:00:00Z")
        self.assertEqual(new, old)
        new.data.clear()
        self.assertEqual(self.metrics.report(), old)

    def test_publication_cuts_rollback_and_post_commit_replay(self):
        old = self.publish()
        self.ingest(self.stages())
        for cut in ("after_input_snapshot", "after_report", "after_head"):

            def fail(name):
                if name == cut:
                    raise OSError(cut)

            with patch.object(self.metrics, "_stage", fail):
                with self.assertRaises(OSError):
                    self.publish()
            self.assertEqual(self.metrics.report(), old)
            self.assertEqual(self.metrics.status()["state"], "pending")

        def after_commit(name):
            if name == "after_commit":
                raise OSError(name)

        with patch.object(self.metrics, "_stage", after_commit):
            with self.assertRaises(OSError):
                self.publish()
        committed = self.metrics.report()
        self.assertEqual(self.publish(), committed)

    def test_report_cache_deletion_rebuild_and_retained_snapshot_replay(self):
        self.ingest(self.stages())
        old = self.publish()
        material = self.metrics.inputs(old.report_id)
        state = reconstruct(material["facts"], material["documents"], material["health"])
        data = reconstruct_timing(
            material["facts"], material["documents"], state, report_as_of=AS_OF
        )
        data.update(
            document_rejections=material["document_rejections"], versions=old.data["versions"]
        )
        self.assertEqual(data, old.data)
        with self.store.transaction():
            self.store._db().execute("DELETE FROM timing_head")
            self.store._db().execute("DELETE FROM timing_reports")
        self.assertEqual(self.publish().report_id, old.report_id)

    def test_backup_restore_retains_all_reports_and_inputs(self):
        self.ingest(self.stages()[:1])
        old = self.publish()
        recovery = RecoveryStore(self.root / "recovery")
        backup = recovery.backup(self.store)
        restored = self.root / "restored.sqlite"
        recovery.restore(backup, restored)
        with RawStore(restored) as store:
            metrics = MetricsEngine(store)
            self.assertEqual(metrics.report(), old)
            self.assertEqual(metrics.inputs(old.report_id), self.metrics.inputs(old.report_id))

    def test_output_budget_preserves_previous_head(self):
        old = self.publish()
        self.ingest(self.stages())
        self.metrics.max_output_bytes = 10
        with self.assertRaises(ProjectionError):
            self.publish()
        self.assertEqual(self.metrics.report(), old)

    def test_sqlite_commit_busy_preserves_report_and_pending_watermark(self):
        old = self.publish()
        self.ingest(self.stages())
        db = sqlite3.connect(self.root / "facts.sqlite")
        self.addCleanup(db.close)
        db.execute("BEGIN")
        db.execute("SELECT * FROM timing_reports").fetchall()
        self.store._db().execute("PRAGMA busy_timeout=1")
        with self.assertRaises(sqlite3.OperationalError):
            self.publish()
        db.rollback()
        self.assertEqual(self.metrics.report(), old)
        self.assertEqual(self.metrics.status()["state"], "pending")

    def test_cycle_window_uses_business_cycle_not_execution_date(self):
        self.ingest(self.process())
        report = self.publish(cycle_window=("2026-10-09T00:00:00Z", "2026-10-10T00:00:00Z"))
        self.assertEqual(report.data["observed_runs"], 1)
        report = self.publish(cycle_window=("2026-10-10T00:00:00Z", "2026-10-11T00:00:00Z"))
        self.assertEqual(report.data["observed_runs"], 0)
        self.assertEqual(report.data["intervals"], [])

    def test_rule_platform_and_configuration_stratification(self):
        self.ingest(self.stages())
        self.publish()
        stratum = self.stats("stage")[0]["stratum"]
        self.assertEqual(stratum["platform_id"], self.run["platform_id"])
        self.assertEqual(stratum["definition_ref"], self.run["definition_ref"])
        self.assertEqual(stratum["config_refs"], self.run["config_refs"])
        self.assertEqual(stratum["purpose"], self.run["purpose"])
        self.assertEqual(stratum["parameters"], self.context["parameters"])

    def test_conflicting_event_id_and_parent_mismatch_not_timed(self):
        events = self.stages()
        self.ingest(events)
        other = copy.deepcopy(events[1])
        other["payload"]["result"] = "failure"
        self.ingest([other])
        report = self.publish()
        self.assertTrue(report.data["diagnostics"])
        self.assertFalse(any(r["duration_ns"] is not None for r in self.rows("stage")))

    def test_large_duration_mean_uses_exact_rational_strings(self):
        self.ingest(self.stages("a", duration=LIMIT) + self.stages("b", duration=LIMIT - 1))
        self.publish()
        self.assertEqual(
            self.stats("stage")[0]["statistics"]["mean_ns"],
            {"numerator": str(2 * LIMIT - 1), "denominator": "2"},
        )

    def test_signal_exit_retains_failure_duration(self):
        events = self.process()
        events[-1]["payload"] = {
            "termination": "signaled",
            "signal": 9,
            "process_role": self.context["process_role"],
        }
        self.ingest(events)
        self.publish()
        self.assertEqual(self.rows("process_execution")[0]["duration_ns"], 2_000_000_000)
        self.assertEqual(self.stats("process_execution")[0]["statistics"]["failure_n"], 1)

    def test_process_invocations_cannot_pair_across_sessions(self):
        events = self.process()
        events[-1]["source"]["session_id"] = "another-invocation"
        self.ingest(events)
        self.publish()
        self.assertEqual(self.rows("process_execution")[0]["pairing"], "conflict")
        self.assertIsNone(self.rows("process_execution")[0]["duration_ns"])
        self.engine.rebuild(published_at=LATER)
        self.assertEqual(
            self.engine.get("attempts", self.context["attempt_id"])["execution"]["state"],
            "conflict",
        )

    def test_process_pairing_consumers_agree_on_shared_facts(self):
        documents = [
            self.bundle[k] for k in ("definition_manifest", "run_context", "attempt_context")
        ]
        for mode, expected_state, expected_pairing in (
            ("normal", "exited", "paired"),
            ("source", "conflict", "conflict"),
            ("session", "conflict", "conflict"),
            ("boot", "conflict", "conflict"),
            ("role", "conflict", "conflict"),
            ("two_starts", "conflict", "conflict"),
            ("start_only", "started", "start_only"),
            ("end_only", "end_only", "end_only"),
        ):
            with self.subTest(mode=mode):
                events = self.process()
                if mode in ("source", "session", "boot"):
                    field = {"source": "source_id", "session": "session_id", "boot": "boot_id"}[
                        mode
                    ]
                    events[1]["source"]["boot_id"] = "boot-a" if mode == "boot" else None
                    events[2]["source"][field] = "other"
                elif mode == "two_starts":
                    events.append(
                        self.boundary(
                            "attempt.started",
                            {
                                "invocation_id": "other",
                                "process_role": self.context["process_role"],
                            },
                        )
                    )
                elif mode == "role":
                    events[2]["payload"]["process_role"] = "other"
                elif mode == "start_only":
                    events.pop()
                elif mode == "end_only":
                    events.pop(1)
                facts = [{"event": e, "hash": canonical_hash(e), "status": "known"} for e in events]
                state = reconstruct(facts, documents, [])
                timing = reconstruct_timing(facts, documents, state, report_as_of=AS_OF)
                self.assertEqual(state["attempts"][0]["execution"]["state"], expected_state)
                rows = [r for r in timing["intervals"] if r["metric"] == "process_execution"]
                self.assertEqual(len(rows), 2 if mode == "role" else 1)
                self.assertEqual({row["pairing"] for row in rows}, {expected_pairing})

    def test_distinct_attempts_do_not_join_same_interval_id(self):
        c = self.context_for("other", retry=self.context["attempt_id"])
        self.engine.import_documents([projections.docrecord(c)])
        events = self.stages()
        events[1]["association"].update(
            attempt_id=c["attempt_id"], task_instance_id=c["task_instance_id"]
        )
        self.ingest(events)
        self.publish()
        self.assertEqual({r["pairing"] for r in self.rows("stage")}, {"start_only", "end_only"})

    def test_same_interval_conflicting_endpoints_are_not_averaged(self):
        events = self.stages()
        other = copy.deepcopy(events[1])
        other["event_id"] = "00000000-0000-4000-8000-000000001111"
        other["occurred_at"] = "2026-10-10T23:59:59Z"
        self.ingest(events + [other])
        self.publish()
        row = self.rows("stage")[0]
        self.assertEqual(row["pairing"], "conflict")
        self.assertEqual(len(row["because"]), 3)

    def test_different_platform_and_config_do_not_mix_statistics(self):
        self.ingest(self.process())
        rc = copy.deepcopy(self.run)
        rc["run_id"] = "run_" + "a" * 64
        rc["platform_id"] = "other-platform"
        rc["config_refs"] = [{"id": "other-config", "sha256": "a" * 64}]
        projections.reseal(rc)
        c = self.context_for("other-run", run=rc)
        self.engine.import_documents([projections.docrecord(rc), projections.docrecord(c)])
        self.ingest(self.process(context=c, code=2))
        self.publish()
        stats = self.stats("process_execution")
        self.assertEqual(len(stats), 2)
        self.assertEqual({s["stratum"]["platform_id"] for s in stats}, {"wsl2", "other-platform"})
        self.assertEqual(sorted(s["statistics"]["n"] for s in stats), [1, 1])

    def test_late_acceptance_resolves_submission_unknown_without_resubmission(self):
        self.ingest(self.submission("unknown"))
        old = self.publish()
        accepted = self.submission("accepted")[-1]
        self.ingest([accepted])
        self.publish()
        self.assertEqual(self.rows("submission_processing")[0]["duration_ns"], 2_000_000_000)
        self.assertEqual(self.rows("submission_processing", old)[0]["pairing"], "start_only")

    def test_all_interval_conclusions_reference_retained_input(self):
        self.ingest(self.process() + self.stages())
        report = self.publish()
        material = self.metrics.inputs(report.report_id)
        refs = {event_ref(f) for f in material["facts"]}
        from sledra_analysis.projections.reducer import doc_ref

        refs |= {doc_ref(d) for d in material["documents"]}
        for row in report.data["intervals"]:
            self.assertTrue(row["because"])
            self.assertLessEqual(set(row["because"]), refs)

    def test_accounting_input_and_output_budgets_rollback(self):
        record = self.accounting()
        old = self.publish()
        self.metrics.max_documents = 1
        with self.assertRaises(ProjectionError):
            self.publish(accounting=[record, record])
        self.metrics.max_documents = 10000
        self.metrics.max_input_bytes = 10
        with self.assertRaises(ProjectionError):
            self.publish(accounting=[record])
        self.assertEqual(self.metrics.report(), old)

    def test_sigkill_timing_publication_is_atomic(self):
        old = self.publish()
        self.ingest(self.stages())
        self.store.close()
        for cut in ("after_input_snapshot", "after_report", "after_head", "after_commit"):
            target = self.root / (cut + ".sqlite")
            with (
                sqlite3.connect(self.root / "facts.sqlite") as source,
                sqlite3.connect(target) as destination,
            ):
                source.backup(destination)
            script = """import os,signal,sys
from pathlib import Path
from sledra_analysis.store.raw import RawStore
from sledra_analysis.metrics.engine import MetricsEngine
with RawStore(Path(sys.argv[1])) as store:
    engine=MetricsEngine(store)
    def crash(name):
        if name == sys.argv[2]: os.kill(os.getpid(),signal.SIGKILL)
    engine._stage=crash
    engine.publish(report_as_of=sys.argv[3],published_at=sys.argv[4])
"""
            process = subprocess.run(
                [sys.executable, "-c", script, str(target), cut, AS_OF, LATER], capture_output=True
            )
            self.assertEqual(process.returncode, -signal.SIGKILL, process.stderr.decode())
            with RawStore(target) as store:
                engine = MetricsEngine(store)
                if cut == "after_commit":
                    self.assertNotEqual(engine.report().report_id, old.report_id)
                    self.assertEqual(engine.status()["state"], "current")
                else:
                    self.assertEqual(engine.report(), old)
                    self.assertEqual(engine.status()["state"], "pending")
                final = engine.publish(report_as_of=AS_OF, published_at=LATER)
                self.assertEqual(engine.publish(report_as_of=AS_OF, published_at=LATER), final)
            PROPERTY_OBSERVATIONS.append({"cut": cut, "signal": "SIGKILL", "recovered": True})

    def test_unknown_cycle_is_counted_as_unlocated_with_window(self):
        rc = copy.deepcopy(self.run)
        rc["run_id"] = "run_" + "b" * 64
        rc["cycle"]["cycle_time"] = "2026-10-09T16:00:00-00:00"
        projections.reseal(rc)
        self.engine.import_documents([projections.docrecord(rc)])
        report = self.publish(cycle_window=("2026-10-09T00:00:00Z", "2026-10-10T00:00:00Z"))
        self.assertEqual(report.data["unlocated_cycle_run_ids"], [rc["run_id"]])
        self.assertTrue(
            any(d["reason"] == "cycle_time_unavailable" for d in report.data["diagnostics"])
        )

    def test_accounting_rules_and_hold_policy_have_separate_denominators(self):
        record = self.accounting()
        other = copy.deepcopy(record)
        other.update(
            interval_id="queue-b",
            adapter_version="synthetic-accounting-2",
            includes_hold=True,
            queue_basis="accepted",
        )
        self.publish(accounting=[record, other])
        stats = self.stats("scheduler_accounting_queue")
        self.assertEqual(len(stats), 2)
        self.assertEqual({s["stratum"]["includes_hold"] for s in stats}, {False, True})
        self.assertEqual([s["statistics"]["n"] for s in stats], [1, 1])

    def test_quarantine_and_pending_source_bytes_remain_in_timing_report(self):
        shared = self.root / "shared"
        shared.mkdir()
        path = shared / "a.open.jsonl"
        path.write_bytes(
            segment(b"".join(canonical_bytes(e) + b"\n" for e in self.process()) + b"bad\npartial")
        )
        ingest = AtomicIngestor(self.store, shared, "timing-root")
        ingest.scan(path.name, collected_at=LATER)
        first = self.publish()
        flags = {flag for source in first.data["source_health"] for flag in source["diagnostics"]}
        self.assertLessEqual({"quarantined_records", "pending_source_bytes"}, flags)
        with path.open("ab") as stream:
            stream.write(b"\n")
        ingest.scan(path.name, collected_at=LATER)
        second = self.publish()
        self.assertNotEqual(first.report_id, second.report_id)
        self.assertNotIn(
            "pending_source_bytes",
            {flag for source in second.data["source_health"] for flag in source["diagnostics"]},
        )

    def test_registry_quarantine_changes_both_health_snapshots(self):
        self.ingest(self.process())
        old_projection = self.engine.rebuild(published_at=LATER)
        old_report = self.publish()
        shared = self.root / "shared"
        shared.mkdir()
        (shared / "bad.json").write_bytes(b"{broken")
        isolated = AtomicIngestor(self.store, shared, "root").registry(
            "bad.json", source_id="registry", collected_at=LATER
        )
        self.assertIsNotNone(isolated.quarantine_id)
        self.assertEqual(self.engine.status()["state"], "pending")
        self.assertEqual(self.metrics.status()["state"], "pending")
        new_projection = self.engine.rebuild(published_at=LATER)
        new_report = self.publish()
        self.assertNotEqual(old_projection.revision_id, new_projection.revision_id)
        self.assertNotEqual(old_report.report_id, new_report.report_id)
        for health in (new_projection.data["source_health"], new_report.data["source_health"]):
            self.assertEqual(len(health), 1)
            self.assertEqual(health[0]["run_ids"], [])
            self.assertEqual(health[0]["quarantine_count"], 1)
            self.assertIn("quarantined_records", health[0]["diagnostics"])
        self.assertEqual(self.engine.revision(old_projection.revision_id), old_projection)
        self.assertEqual(self.metrics.report(old_report.report_id), old_report)

        (shared / "bad.sealed.jsonl").write_bytes(segment(b"{broken\n"))
        AtomicIngestor(self.store, shared, "root").scan("bad.sealed.jsonl", collected_at=LATER)
        with_segment = self.publish()
        by_kind = {item["id"].split(":", 1)[0]: item for item in with_segment.data["source_health"]}
        self.assertEqual(set(by_kind), {"registry", "source"})
        self.assertEqual(by_kind["registry"]["run_ids"], [])
        self.assertEqual(by_kind["source"]["quarantine_count"], 1)

    def test_completed_scan_batch_size_does_not_change_final_ids(self):
        shared = self.root / "shared"
        shared.mkdir()
        raw = segment(b"".join(canonical_bytes(e) + b"\n" for e in self.process()))
        (shared / "a.sealed.jsonl").write_bytes(raw)
        results = []
        for size in (1, 1024):
            with RawStore(self.root / f"batch-{size}.sqlite") as store:
                engine = projections.ProjectionEngine(store)
                engine.import_documents(
                    [
                        projections.docrecord(self.bundle[k])
                        for k in ("definition_manifest", "run_context", "attempt_context")
                    ]
                )
                ingestor = AtomicIngestor(store, shared, "root", max_records=size)
                for _ in range(5):
                    scanned = ingestor.scan("a.sealed.jsonl", collected_at=LATER)
                    if scanned.checkpoint.committed_offset == len(raw):
                        break
                self.assertEqual(scanned.checkpoint.committed_offset, len(raw))
                history = json.loads(
                    store._db().execute("SELECT diagnostics FROM checkpoints").fetchone()[0]
                )
                self.assertEqual("scan_record_budget_reached" in history, size == 1)
                projection = engine.rebuild(published_at=LATER)
                report = MetricsEngine(store).publish(report_as_of=AS_OF, published_at=LATER)
                results.append(
                    (projection.revision_id, projection.data, report.report_id, report.data)
                )
        self.assertEqual(results[0], results[1])

    def test_scan_history_does_not_change_semantic_snapshot_hash(self):
        shared = self.root / "shared"
        shared.mkdir()
        (shared / "a.sealed.jsonl").write_bytes(segment(b""))
        AtomicIngestor(self.store, shared, "root").scan("a.sealed.jsonl", collected_at=LATER)

        def semantic_hash():
            with self.store.transaction():
                return canonical_hash(
                    list(
                        read_inputs(
                            self.store._db(),
                            self.engine.validator,
                            self.engine.document_validator,
                            max_facts=self.engine.max_facts,
                            max_documents=self.engine.max_documents,
                            max_input_bytes=self.engine.max_input_bytes,
                        )
                    )
                )

        before = semantic_hash()
        with self.store.transaction():
            self.store._db().execute(
                "UPDATE checkpoints SET diagnostics=?",
                (json.dumps(["scan_record_budget_reached"]),),
            )
        self.assertEqual(before, semantic_hash())
        self.assertEqual(
            json.loads(
                self.store._db().execute("SELECT diagnostics FROM checkpoints").fetchone()[0]
            ),
            ["scan_record_budget_reached"],
        )

    def test_registry_locator_conflict_and_replay_preserve_health_identity(self):
        shared = self.root / "shared"
        shared.mkdir()
        path = shared / "bad.json"
        ingestor = AtomicIngestor(self.store, shared, "root")
        path.write_bytes(b"{broken")
        ingestor.registry(path.name, source_id="registry", collected_at=LATER)
        first = self.publish()
        path.write_bytes(b"{different")
        ingestor.registry(path.name, source_id="registry", collected_at=LATER)
        second = self.publish()
        health = second.data["source_health"]
        self.assertEqual(len(health), 1)
        self.assertEqual(health[0]["quarantine_count"], 2)
        self.assertEqual(
            set(health[0]["diagnostics"]),
            {"quarantined_records", "registry_immutable_conflict"},
        )
        self.assertNotEqual(first.report_id, second.report_id)
        self.assertEqual(self.publish(), second)


class TimingReducerTests(unittest.TestCase):
    event = projections.ProjectionTests.event
    register = projections.ProjectionTests.register
    set_up_facts = projections.ProjectionTests.set_up_facts
    boundary = TimingTests.boundary
    process = TimingTests.process
    stages = TimingTests.stages

    def setUp(self):
        self.set_up_facts()

    def test_unknown_timezone_and_excess_precision_unavailable(self):
        for stamp in (
            "2026-10-11T00:00:00",
            "2026-10-11T00:00:00-00:00",
            "2026-10-11T00:00:00.1234567891Z",
            "2026-10-11T00:00:00+01:99",
        ):
            with self.subTest(stamp=stamp):
                with self.assertRaises(ValueError):
                    utc_ns(stamp)
                self.assertEqual(
                    wall_interval({"occurred_at": stamp}, {"occurred_at": AS_OF})["quality"],
                    "unavailable",
                )

    def test_permutation_duplicates_and_late_prefix_property(self):
        events = self.process(code=2) + self.stages("a", duration=10) + self.stages("b")[:1]
        facts = [
            {"event": e, "hash": canonical_hash(e), "status": "known", "reason": None}
            for e in events
        ]
        documents = [
            self.bundle[k] for k in ("definition_manifest", "run_context", "attempt_context")
        ]

        def reduce(items):
            dedup = list({event_ref(f): f for f in items}.values())
            return reconstruct_timing(
                dedup, documents, reconstruct(dedup, documents, []), report_as_of=AS_OF
            )

        expected = reduce(facts)
        rng = random.Random(15)
        for _ in range(60):
            shuffled = facts + rng.sample(facts, 3)
            rng.shuffle(shuffled)
            reduce(shuffled[: rng.randrange(len(shuffled))])
            self.assertEqual(reduce(shuffled), expected)
        for order in itertools.permutations(facts[:3]):
            self.assertEqual(reduce(list(order) + facts[3:]), expected)
        PROPERTY_OBSERVATIONS.append(
            {
                "seed": 15,
                "permutations": 60,
                "boundary_permutations": 6,
                "duplicates_and_late_prefix": True,
            }
        )


if __name__ == "__main__":
    unittest.main()
