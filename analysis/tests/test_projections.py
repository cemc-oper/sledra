"""V05/V07/V08/V09: parent resolution, causal revisions and replay invariance."""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import random
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from store_fixture import frozen_records
from test_source import segment
from test_store import COLLECTED, LATER, ROOT, fixture, record

from sledra_analysis.ingest.transaction import AtomicIngestor
from sledra_analysis.projections.documents import (
    DocumentRecord,
    DocumentValidator,
    KINDS,
    content_hash,
    instance_id,
    length_digest,
)
from sledra_analysis.projections.engine import ProjectionEngine, ProjectionError
from sledra_analysis.store.canonical import RecordError, canonical_bytes, canonical_hash
from sledra_analysis.store.raw import RawStore
from sledra_analysis.store.recovery import RecoveryStore

PROPERTY_OBSERVATIONS = []


def reseal(doc):
    if doc["kind"] in ("run_context", "attempt_context"):
        doc["context_sha256"] = content_hash(doc, "context_sha256")
    return doc


def docrecord(value, locator=None):
    return DocumentRecord(
        canonical_bytes(value), locator or value[KINDS[value["kind"]]] + ".json", COLLECTED
    )


class ProjectionTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="sledra V05 V07 V08 V09 private ")
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.store = RawStore(self.root / "facts.sqlite")
        self.addCleanup(self.store.close)
        self.engine = ProjectionEngine(self.store)
        self.bundle = fixture("valid-bundle.json")
        self.context = self.bundle["attempt_context"]
        self.run = self.bundle["run_context"]
        self.catalog = json.loads((ROOT / "spec/1.0.0/event-types.json").read_bytes())
        self.serial = 0
        self.engine.import_documents(
            [
                docrecord(self.bundle[k])
                for k in ("definition_manifest", "run_context", "attempt_context")
            ]
        )

    def event(self, name, payload, *, context=None, assoc=None, **updates):
        context = context or self.context
        self.serial += 1
        e = copy.deepcopy(self.bundle["events"][0])
        e.update(
            event_id=f"00000000-0000-4000-8000-{self.serial:012x}", event_type=name, payload=payload
        )
        entry = self.catalog.get(name)
        e["producer"]["role"] = entry["roles"][0] if entry else "future_owner"
        e["source"] = {
            "source_id": "test-node",
            "session_id": "projection-test",
            "kind": entry["source_kinds"][0] if entry else "direct",
        }
        e["association"] = {"run_id": context["run_id"]}
        if entry and entry["association"] in ("instance", "attempt"):
            e["association"]["task_instance_id"] = context["task_instance_id"]
        if entry and entry["association"] == "attempt":
            e["association"]["attempt_id"] = context["attempt_id"]
        if assoc is not None:
            e["association"] = assoc
        if entry and entry["evidence_required"]:
            e["evidence_refs"] = [
                {
                    "evidence_id": "evidence-" + str(self.serial),
                    "locator": "opaque:synthetic",
                    "source": "synthetic",
                    "availability": "present",
                }
            ]
        e.update(updates)
        return e

    def context_for(self, label, *, retry=None, member="01", run=None):
        c = copy.deepcopy(self.context)
        c["attempt_id"] = "att_" + hashlib.sha256(label.encode()).hexdigest()
        c["execution_token_sha256"] = hashlib.sha256(("token:" + label).encode()).hexdigest()
        c["retry_of"] = retry
        c["parameters"]["member"] = member
        if run:
            c["run_id"] = run["run_id"]
            c["run_context_sha256"] = run["context_sha256"]
        c["task_instance_id"] = instance_id(c["run_id"], c["task_key"], c["parameters"])
        return reseal(c)

    def register(self, c=None):
        c = c or self.context
        return self.event(
            "attempt.registered",
            {
                "attempt_context_sha256": c["context_sha256"],
                "execution_token_sha256": c["execution_token_sha256"],
                "retry_of": c["retry_of"],
            },
            context=c,
        )

    def process(self, c=None, code=0):
        c = c or self.context
        return [
            self.register(c),
            self.event(
                "attempt.started",
                {"invocation_id": c["attempt_id"], "process_role": c["process_role"]},
                context=c,
            ),
            self.event(
                "attempt.exited",
                {"termination": "exited", "exit_code": code, "process_role": c["process_role"]},
                context=c,
            ),
        ]

    def finish(self, ids, *, declaration="finish", scope="scope-a", result="finished"):
        ids = sorted(ids)
        return self.event(
            "run.finish_declared",
            {
                "declaration_id": declaration,
                "scope_ref": scope,
                "rule_version": "1",
                "covered_attempt_ids": ids,
                "covered_attempts_sha256": canonical_hash(ids),
                "reason": "synthetic",
                "result": result,
            },
        )

    def choose(self, selected, candidates, *, name="selection", supersedes=None):
        p = {
            "selection_id": name,
            "selected_attempt_id": selected,
            "candidate_attempt_ids": candidates,
            "reason": "synthetic",
        }
        if supersedes:
            p["supersedes_selection_id"] = supersedes
        return self.event("attempt.selection_declared", p)

    def ingest(self, events):
        self.store.ingest([record(e, locator=e["event_id"] + ".jsonl") for e in events])

    def rebuild(self):
        return self.engine.rebuild(published_at=LATER)

    def attempt(self, c=None):
        return self.engine.get("attempts", (c or self.context)["attempt_id"])

    def instance(self, c=None):
        return self.engine.get("instances", (c or self.context)["task_instance_id"])

    def run_projection(self):
        return self.engine.get("runs", self.run["run_id"])

    def test_frozen_19_facts_reconstruct_two_runs_and_four_attempts(self):
        records, sources = frozen_records(self.root / "shared")
        self.store.close()
        with RawStore(self.root / "frozen.sqlite") as store:
            store.ingest(records)
            engine = ProjectionEngine(store)
            engine.import_documents(
                [
                    DocumentRecord(raw, p, COLLECTED)
                    for p, raw in sources.items()
                    if p.endswith(".json") and json.loads(raw).get("kind") == "definition_manifest"
                ]
            )
            revision = engine.rebuild(published_at=LATER)
            self.assertEqual(
                [len(revision.data[k]) for k in ("runs", "instances", "attempts")], [2, 2, 4]
            )
            retry = next(r for r in revision.data["runs"] if len(r["attempt_ids"]) == 3)
            self.assertEqual(retry["lifecycle"]["state"], "needs_finalization")
            summary = next(i for i in revision.data["instances"] if len(i["attempt_ids"]) == 3)
            self.assertEqual(summary["execution"]["state"], "rerunning")
            self.assertEqual(store.counts().events, 19)
            self.assertEqual(engine.status()["state"], "current")

    def test_failure_then_success_retains_attempts_and_selects_causal_leaf(self):
        second = self.context_for("retry", retry=self.context["attempt_id"])
        self.engine.import_documents([docrecord(second)])
        self.ingest(self.process(code=7) + self.process(second, 0))
        self.rebuild()
        self.assertEqual(self.instance()["execution"]["state"], "recovered")
        self.assertEqual(self.instance()["current_attempt_id"], second["attempt_id"])
        self.assertEqual(self.attempt()["execution"]["exit_code"], 7)
        self.assertEqual(self.attempt(second)["execution"]["exit_code"], 0)
        self.assertEqual(len(self.instance()["attempt_ids"]), 2)

    def test_success_then_registered_or_started_retry_is_rerunning(self):
        second = self.context_for("retry", retry=self.context["attempt_id"])
        self.engine.import_documents([docrecord(second)])
        self.ingest(self.process() + [self.register(second)])
        old = self.rebuild()
        self.assertEqual(self.instance()["execution"]["state"], "rerunning")
        self.ingest([self.process(second)[1]])
        new = self.rebuild()
        self.assertEqual(self.instance()["execution"]["state"], "rerunning")
        self.assertNotEqual(old.revision_id, new.revision_id)
        self.assertEqual(self.engine.revision(old.revision_id), old)

    def test_end_only_start_only_and_signal_preserve_missing_boundaries(self):
        events = self.process()
        self.ingest([events[0], events[2]])
        self.rebuild()
        self.assertEqual(self.attempt()["execution"]["state"], "end_only")
        self.assertIn("missing_start", self.attempt()["observability"]["flags"])
        self.assertNotIn("duration", self.attempt()["execution"])
        self.ingest([events[1]])
        self.rebuild()
        self.assertEqual(self.attempt()["execution"]["state"], "exited")
        self.assertNotIn("missing_start", self.attempt()["observability"]["flags"])
        second = self.context_for("signal", member="02")
        self.engine.import_documents([docrecord(second)])
        process = self.process(second)
        self.ingest(process[:2])
        self.rebuild()
        self.assertEqual(self.attempt(second)["execution"]["state"], "started")
        self.assertIn("missing_end", self.attempt(second)["observability"]["flags"])
        self.ingest(
            [
                self.event(
                    "attempt.exited",
                    {
                        "termination": "signaled",
                        "signal": 9,
                        "process_role": second["process_role"],
                    },
                    context=second,
                )
            ]
        )
        self.rebuild()
        self.assertEqual(self.attempt(second)["execution"]["state"], "signaled")

    def test_same_cycle_distinct_run_and_parameterized_instances_never_merge(self):
        other = copy.deepcopy(self.run)
        other["run_id"] = "run_" + "b" * 64
        reseal(other)
        second = self.context_for("other-run", run=other)
        member = self.context_for("member", member="02")
        self.engine.import_documents([docrecord(other), docrecord(second), docrecord(member)])
        self.ingest(self.process() + self.process(second) + self.process(member))
        result = self.rebuild()
        self.assertEqual(len(result.data["runs"]), 2)
        self.assertNotEqual(self.instance()["id"], self.instance(member)["id"])
        self.assertNotEqual(self.instance()["id"], self.instance(second)["id"])

    def test_fork_requires_selection_and_does_not_choose_latest_timestamp(self):
        second = self.context_for("fork", retry=None)
        self.engine.import_documents([docrecord(second)])
        self.ingest(self.process() + self.process(second, 7))
        self.rebuild()
        self.assertIsNone(self.instance()["current_attempt_id"])
        self.assertEqual(self.instance()["execution"]["state"], "ambiguous")
        self.ingest(
            [
                self.choose(
                    self.context["attempt_id"], [self.context["attempt_id"], second["attempt_id"]]
                )
            ]
        )
        self.rebuild()
        self.assertEqual(self.instance()["current_attempt_id"], self.context["attempt_id"])
        self.assertEqual(self.instance()["selection"]["state"], "explicit_selection")

    def test_new_attempt_invalidates_selection_and_explicit_successor_can_reselect(self):
        second = self.context_for("fork")
        self.engine.import_documents([docrecord(second)])
        self.ingest(
            self.process()
            + self.process(second, 7)
            + [
                self.choose(
                    self.context["attempt_id"],
                    [self.context["attempt_id"], second["attempt_id"]],
                    name="s1",
                )
            ]
        )
        self.rebuild()
        third = self.context_for("later", retry=second["attempt_id"])
        self.engine.import_documents([docrecord(third)])
        self.ingest([self.register(third)])
        self.rebuild()
        self.assertIsNone(self.instance()["current_attempt_id"])
        self.ingest(
            [
                self.choose(
                    third["attempt_id"],
                    [self.context["attempt_id"], second["attempt_id"], third["attempt_id"]],
                    name="s2",
                    supersedes="s1",
                )
            ]
        )
        self.rebuild()
        self.assertEqual(self.instance()["current_attempt_id"], third["attempt_id"])

    def test_unrelated_conflicting_selection_missing_parent_and_cycle_are_ambiguous(self):
        second = self.context_for("fork")
        self.engine.import_documents([docrecord(second)])
        candidates = [self.context["attempt_id"], second["attempt_id"]]
        self.ingest(
            self.process()
            + self.process(second)
            + [
                self.choose(candidates[0], candidates, name="s1"),
                self.choose(candidates[1], candidates, name="s2"),
            ]
        )
        self.rebuild()
        self.assertIsNone(self.instance()["current_attempt_id"])
        self.assertEqual(self.instance()["selection"]["state"], "ambiguous")

    def test_selection_of_foreign_instance_is_rejected_without_discarding_raw(self):
        foreign = self.context_for("foreign", member="02")
        self.engine.import_documents([docrecord(foreign)])
        invalid = self.choose(
            foreign["attempt_id"], [self.context["attempt_id"], foreign["attempt_id"]]
        )
        self.ingest(self.process() + self.process(foreign) + [invalid])
        result = self.rebuild()
        self.assertIsNone(self.instance()["current_attempt_id"])
        self.assertTrue(
            any(
                r["fact"].startswith(invalid["event_id"] + "@")
                for r in result.data["rejected_facts"]
            )
        )
        self.assertIsNotNone(self.store.get_event(invalid["event_id"]))

    def test_retry_missing_foreign_parent_and_cycles_never_create_current_success(self):
        second = self.context_for("cycle", retry=self.context["attempt_id"])
        first = copy.deepcopy(self.context)
        first["retry_of"] = second["attempt_id"]
        reseal(first)
        with RawStore(self.root / "cycle.sqlite") as store:
            engine = ProjectionEngine(store)
            engine.import_documents(
                [docrecord(self.bundle[k]) for k in ("definition_manifest", "run_context")]
                + [docrecord(first), docrecord(second)]
            )
            store.ingest(
                [
                    record(e, locator=e["event_id"])
                    for e in self.process(first) + self.process(second)
                ]
            )
            engine.rebuild(published_at=LATER)
            self.assertEqual(
                engine.get("instances", first["task_instance_id"])["execution"]["state"],
                "ambiguous",
            )
        orphan = self.context_for("missing", retry="att_" + "f" * 64, member="02")
        self.engine.import_documents([docrecord(orphan)])
        self.ingest(self.process(orphan))
        self.rebuild()
        self.assertEqual(self.instance(orphan)["execution"]["state"], "ambiguous")
        self.assertIn("orphan", self.attempt(orphan)["observability"]["flags"])

    def test_finish_then_rerun_revises_original_run_and_preserves_old_scope(self):
        self.ingest(self.process() + [self.finish([self.context["attempt_id"]])])
        first = self.rebuild()
        self.assertEqual(self.run_projection()["lifecycle"]["state"], "finish_declared")
        later = self.context_for("later", retry=self.context["attempt_id"])
        self.engine.import_documents([docrecord(later)])
        self.ingest([self.register(later)])
        second = self.rebuild()
        self.assertEqual(len(second.data["runs"]), 1)
        self.assertEqual(self.run_projection()["lifecycle"]["state"], "needs_finalization")
        self.ingest(
            [
                self.finish(
                    [self.context["attempt_id"], later["attempt_id"]], declaration="new-finish"
                )
            ]
        )
        third = self.rebuild()
        self.assertEqual(self.run_projection()["lifecycle"]["state"], "finish_declared")
        self.assertEqual(len(self.run_projection()["finish_declarations"]), 2)
        self.assertNotEqual(first.revision_id, third.revision_id)
        self.assertEqual(self.engine.revision(first.revision_id), first)

    def test_conflicting_finish_sets_results_and_unresolved_coverage_are_preserved(self):
        self.ingest(
            self.process()
            + [
                self.finish([self.context["attempt_id"]]),
                self.finish([self.context["attempt_id"]], declaration="other", result="abandoned"),
            ]
        )
        self.rebuild()
        self.assertEqual(self.run_projection()["lifecycle"]["state"], "conflict")
        self.assertEqual(len(self.run_projection()["finish_declarations"]), 2)

    def test_engine_complete_job_done_and_process_zero_do_not_confirm_products(self):
        self.engine.import_documents([docrecord(self.bundle["job_ref"])])
        job = self.bundle["job_ref"]["job_ref_id"]
        engine = self.event(
            "engine.state_observed",
            {
                "engine_instance": "ecflow-test",
                "node_path": "/x",
                "raw_state": "complete",
                "observation_time": LATER,
                "source_position": "line:1",
            },
        )
        control = self.event(
            "engine.control_observed", {"operation": "force_complete", "audit_ref": "opaque:audit"}
        )
        observed = self.event(
            "job.state_observed",
            {
                "job_ref_id": job,
                "raw_state": "DONE",
                "normalized_state": "finished",
                "observation_time": LATER,
            },
            assoc={"run_id": self.run["run_id"], "job_ref_id": job},
        )
        self.ingest(self.process() + [engine, control, observed])
        self.rebuild()
        self.assertEqual(self.run_projection()["product_state"]["state"], "unknown")
        self.assertEqual(self.run_projection()["product_observations"], [])
        self.assertEqual(len(self.run_projection()["engine_observations"]), 2)
        self.assertEqual(self.engine.get("jobs", job)["scheduler"]["state"], "observed_finished")

    def test_stage_failure_is_independent_of_process_exit_zero(self):
        events = self.process()
        events += [
            self.event("stage.started", {"stage_key": "forecast", "interval_id": "i"}),
            self.event(
                "stage.ended", {"stage_key": "forecast", "interval_id": "i", "result": "failure"}
            ),
        ]
        self.ingest(events)
        self.rebuild()
        self.assertEqual(self.attempt()["execution"]["exit_code"], 0)
        self.assertEqual(self.attempt()["stages"][0]["state"], "failure")
        self.assertEqual(self.run_projection()["product_state"]["state"], "unknown")

    def test_submission_unknown_without_job_remains_observed_and_does_not_run(self):
        e = self.event(
            "submission.failed",
            {
                "submission_instance": "s",
                "outcome": "unknown",
                "failure_stage": "timeout",
                "error_class": "timeout",
            },
        )
        self.ingest([self.register(), e])
        self.rebuild()
        self.assertEqual(self.attempt()["execution"]["state"], "registered_only")
        self.assertEqual(self.attempt()["job_ref_ids"], [])
        self.assertEqual(self.attempt()["submission_observations"][0]["outcome"], "unknown")

    def test_scheduler_id_reuse_is_distinct_and_multiple_primary_jobs_are_uncertain(self):
        first = copy.deepcopy(self.bundle["job_ref"])
        second = copy.deepcopy(first)
        second["submission_instance"] = "submit-b"
        second["job_ref_id"] = "job_" + length_digest(
            *(
                second[k]
                for k in (
                    "platform_id",
                    "scheduler",
                    "cluster_id",
                    "scheduler_job_id",
                    "submission_instance",
                )
            )
        )
        self.engine.import_documents([docrecord(first), docrecord(second)])
        self.ingest(self.process())
        self.rebuild()
        self.assertEqual(len(self.attempt()["job_ref_ids"]), 2)
        self.assertIn("association_uncertain", self.attempt()["observability"]["flags"])

    def test_cross_parent_and_submission_generation_mismatch_are_not_semantic_winners(self):
        self.engine.import_documents([docrecord(self.bundle["job_ref"])])
        e = self.event(
            "submission.accepted",
            {
                "submission_instance": "wrong",
                "job_ref_id": self.bundle["job_ref"]["job_ref_id"],
                "confirmation_basis": "observed",
            },
            assoc={
                **self.bundle["events"][1]["association"],
                "job_ref_id": self.bundle["job_ref"]["job_ref_id"],
            },
        )
        self.ingest(self.process() + [e])
        result = self.rebuild()
        self.assertIn("parent_mismatch", self.attempt()["observability"]["flags"])
        self.assertTrue(result.data["rejected_facts"])

    def test_orphan_can_resolve_after_late_parent_import_with_new_revision(self):
        events = self.process()
        with RawStore(self.root / "orphan.sqlite") as store:
            store.ingest([record(e, locator=e["event_id"]) for e in events])
            engine = ProjectionEngine(store)
            first = engine.rebuild(published_at=LATER)
            self.assertIn(
                "orphan",
                engine.get("attempts", self.context["attempt_id"])["observability"]["flags"],
            )
            engine.import_documents(
                [
                    docrecord(self.bundle[k])
                    for k in ("definition_manifest", "run_context", "attempt_context")
                ]
            )
            second = engine.rebuild(published_at=LATER)
            self.assertNotEqual(first.revision_id, second.revision_id)
            self.assertNotIn(
                "orphan",
                engine.get("attempts", self.context["attempt_id"])["observability"]["flags"],
            )
            self.assertEqual(engine.revision(first.revision_id), first)

    def test_unknown_type_invalid_known_fact_and_event_conflict_remain_explained(self):
        events = self.process()
        conflict = copy.deepcopy(events[-1])
        conflict["payload"]["exit_code"] = 7
        opaque = self.event(
            "future.critical", {"reported_success": True}, assoc=events[-1]["association"]
        )
        invalid = self.event(
            "attempt.exited",
            {"termination": "exited", "process_role": self.context["process_role"]},
        )
        self.ingest(events + [conflict, opaque, invalid])
        result = self.rebuild()
        self.assertEqual(self.attempt()["execution"]["state"], "conflict")
        self.assertIn("unsupported_semantics", self.attempt()["observability"]["flags"])
        self.assertEqual(
            {f["disposition"] for f in result.data["facts"]},
            {"known", "conflict", "opaque", "invalid"},
        )
        self.assertEqual(self.run_projection()["product_state"]["state"], "unknown")

    def test_all_conclusions_reference_retained_fact_document_or_source_ids(self):
        self.ingest(self.process() + [self.finish([self.context["attempt_id"]])])
        result = self.rebuild()
        references = {f["ref"] for f in result.data["facts"]}
        references.update(
            "document:" + d["kind"] + ":" + d["id"] + "@" + d["canonical_hash"]
            for d in result.data["input_documents"]
        )
        references.update(h["id"] for h in result.data["source_health"])

        def check(value):
            if isinstance(value, dict):
                if "because" in value:
                    self.assertTrue(value["because"])
                    self.assertLessEqual(set(value["because"]), references)
                for v in value.values():
                    check(v)
            elif isinstance(value, list):
                for v in value:
                    check(v)

        check(result.data)

    def test_document_hash_parameters_and_identity_checks_fail_atomically(self):
        bad = copy.deepcopy(self.context)
        bad["context_sha256"] = "f" * 64
        before = self.store.projection_work()
        with self.assertRaises(RecordError):
            self.engine.import_documents([docrecord(self.bundle["job_ref"]), docrecord(bad)])
        self.assertEqual(self.store.projection_work(), before)
        self.assertEqual(
            self.store._db()
            .execute("SELECT count(*) FROM projection_documents WHERE kind='job_ref'")
            .fetchone()[0],
            0,
        )
        bad = copy.deepcopy(self.context)
        bad["parameters"]["member"] = 1
        reseal(bad)
        with self.assertRaises(RecordError):
            self.engine.import_documents([docrecord(bad)])
        for kind in KINDS:
            self.assertEqual(
                DocumentValidator().validate(canonical_bytes(self.bundle[kind])), self.bundle[kind]
            )

    def test_conflicting_contexts_are_retained_and_never_select_success(self):
        changed = copy.deepcopy(self.context)
        changed["process_role"] = "different"
        reseal(changed)
        self.engine.import_documents([docrecord(changed, locator="changed.json")])
        self.ingest(self.process())
        self.rebuild()
        self.assertIn("conflict", self.attempt()["observability"]["flags"])
        self.assertEqual(self.instance()["execution"]["state"], "ambiguous")
        self.assertEqual(len(self.attempt()["documents"]), 2)

    def test_queue_watermark_and_revision_publish_roll_back_at_all_transaction_cuts(self):
        self.ingest(self.process())
        old = self.rebuild()
        later = self.context_for("later", retry=self.context["attempt_id"])
        self.engine.import_documents([docrecord(later)])
        self.ingest([self.register(later)])
        self.assertEqual(self.engine.status()["state"], "pending")
        for stage in (
            "after_input_snapshot",
            "after_revision",
            "after_work_cursor",
            "before_projection_commit",
        ):

            def fail(name):
                if name == stage:
                    raise OSError("injected publication failure")

            with patch.object(self.engine, "_stage", side_effect=fail), self.assertRaises(OSError):
                self.rebuild()
            self.assertEqual(self.engine.revision(), old)
            self.assertEqual(self.engine.status()["state"], "pending")
        new = self.rebuild()
        self.assertNotEqual(old.revision_id, new.revision_id)
        self.assertEqual(self.engine.status()["state"], "current")

    def test_postcommit_lost_response_and_duplicate_inputs_reuse_immutable_revision(self):
        events = self.process()
        self.ingest(events)

        def fail(name):
            if name == "after_projection_commit":
                raise OSError("lost response")

        with patch.object(self.engine, "_stage", side_effect=fail), self.assertRaises(OSError):
            self.rebuild()
        first = self.engine.revision()
        self.ingest(events)
        second = self.rebuild()
        self.assertEqual(first, second)
        self.assertEqual(
            self.store._db().execute("SELECT count(*) FROM projection_revisions").fetchone()[0], 1
        )
        second.data["runs"].clear()
        self.assertTrue(self.engine.revision().data["runs"])

    def test_deleted_projection_cache_rebuilds_identical_revision_from_raw_and_documents(self):
        self.ingest(self.process())
        expected = self.rebuild()
        with self.store.transaction():
            self.store._db().execute("DELETE FROM projection_heads")
            self.store._db().execute("DELETE FROM projection_revisions")
        self.assertIsNone(self.engine.revision())
        actual = self.rebuild()
        self.assertEqual(expected.revision_id, actual.revision_id)
        self.assertEqual(expected.data, actual.data)
        self.assertEqual(self.store.counts().events, 3)

    def test_consistent_backup_restore_preserves_inputs_revisions_and_pending_queue(self):
        self.ingest(self.process())
        old = self.rebuild()
        self.ingest([self.finish([self.context["attempt_id"]])])
        recovery = RecoveryStore(self.root / "recovery")
        backup = recovery.backup(self.store)
        recovery.restore(backup, self.root / "restore.sqlite")
        with RawStore(self.root / "restore.sqlite") as store:
            engine = ProjectionEngine(store)
            self.assertEqual(engine.revision(), old)
            self.assertEqual(engine.status()["state"], "pending")
            self.assertEqual(engine.rebuild(published_at=LATER).data, self.rebuild().data)

    def test_input_output_budgets_and_commit_failure_keep_previous_projection(self):
        self.ingest(self.process())
        old = self.rebuild()
        with self.assertRaises(ProjectionError):
            ProjectionEngine(self.store, max_facts=1).rebuild(published_at=LATER)
        with self.assertRaises(ProjectionError):
            ProjectionEngine(self.store, max_output_bytes=1).rebuild(published_at=LATER)
        self.assertEqual(self.engine.revision(), old)
        self.ingest([self.finish([self.context["attempt_id"]])])
        reader = sqlite3.connect(self.store.path, isolation_level=None)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM projection_heads").fetchall()
        with self.assertRaises(sqlite3.OperationalError):
            self.rebuild()
        self.assertEqual(self.engine.revision(), old)
        self.assertEqual(self.engine.status()["state"], "pending")
        reader.execute("ROLLBACK")
        self.rebuild()

    def test_source_gap_and_quarantine_are_visible_even_without_new_business_events(self):
        shared = self.root / "shared"
        shared.mkdir()
        events = self.process()
        path = shared / "a.open.jsonl"
        path.write_bytes(
            segment(b"".join(canonical_bytes(e) + b"\n" for e in events) + b"bad\npartial")
        )
        ingest = AtomicIngestor(self.store, shared, "test-root")
        ingest.scan(path.name, collected_at=COLLECTED)
        first = self.rebuild()
        self.assertIn("quarantined_records", self.run_projection()["observability"]["flags"])
        self.assertIn("pending_source_bytes", self.run_projection()["observability"]["flags"])
        with path.open("ab") as f:
            f.write(b"\n")
        ingest.scan(path.name, collected_at=LATER)
        second = self.rebuild()
        self.assertNotEqual(first.revision_id, second.revision_id)
        self.assertNotIn("pending_source_bytes", self.run_projection()["observability"]["flags"])

    def test_seeded_permutations_duplicates_and_late_prefixes_have_identical_final_revision(self):
        events = self.process() + [self.finish([self.context["attempt_id"]])]
        docs = [self.bundle[k] for k in ("definition_manifest", "run_context", "attempt_context")]
        rng = random.Random(14)
        baseline = None
        for index in range(40):
            with RawStore(self.root / f"perm-{index}.sqlite") as store:
                engine = ProjectionEngine(store)
                order = list(events) + rng.choices(events, k=3)
                rng.shuffle(order)
                shuffled = docs[:]
                rng.shuffle(shuffled)
                engine.import_documents([docrecord(d) for d in shuffled])
                store.ingest([record(e, locator=e["event_id"]) for e in order[:2]])
                engine.rebuild(published_at=COLLECTED)
                store.ingest([record(e, locator=e["event_id"]) for e in order[2:]])
                result = engine.rebuild(published_at=LATER)
                current = (result.revision_id, result.data)
                if baseline is None:
                    baseline = current
                self.assertEqual(current, baseline)
        PROPERTY_OBSERVATIONS.append(
            {
                "property": "permutation_duplicate_late_prefix_invariance",
                "seed": 14,
                "trials": 40,
                "pass": True,
                "revision_id": baseline[0],
            }
        )

    def test_exhaustive_start_end_registration_order_has_identical_state(self):
        events = self.process()
        baseline = None
        for index, order in enumerate(itertools.permutations(events)):
            with RawStore(self.root / f"exhaustive-{index}.sqlite") as store:
                engine = ProjectionEngine(store)
                engine.import_documents(
                    [
                        docrecord(self.bundle[k])
                        for k in ("definition_manifest", "run_context", "attempt_context")
                    ]
                )
                for e in order:
                    store.ingest([record(e, locator=e["event_id"])])
                    engine.rebuild(published_at=LATER)
                current = engine.revision()
                if baseline is None:
                    baseline = current
                self.assertEqual(current.revision_id, baseline.revision_id)
                self.assertEqual(current.data, baseline.data)
        PROPERTY_OBSERVATIONS.append(
            {"property": "all_boundary_permutations", "trials": 6, "pass": True}
        )

    def test_process_sigkill_before_and_after_projection_commit_recovers_cursor(self):
        self.ingest(self.process())
        old = self.rebuild()
        self.ingest([self.finish([self.context["attempt_id"]])])
        self.store.close()
        for stage in (
            "after_revision",
            "after_work_cursor",
            "before_projection_commit",
            "after_projection_commit",
        ):
            target = self.root / (stage + ".sqlite")
            with (
                sqlite3.connect(self.root / "facts.sqlite") as source,
                sqlite3.connect(target) as dest,
            ):
                source.backup(dest)
            script = """import os,signal,sys
from pathlib import Path
from sledra_analysis.store.raw import RawStore
from sledra_analysis.projections.engine import ProjectionEngine
with RawStore(Path(sys.argv[1])) as store:
    engine=ProjectionEngine(store)
    def die(name):
        if name==sys.argv[2]:os.kill(os.getpid(),signal.SIGKILL)
    engine._stage=die
    engine.rebuild(published_at="2026-10-14T01:00:00Z")
"""
            result = subprocess.run(
                [sys.executable, "-c", script, str(target), stage], capture_output=True
            )
            self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr.decode())
            with RawStore(target) as store:
                engine = ProjectionEngine(store)
                if stage != "after_projection_commit":
                    self.assertEqual(engine.revision(), old)
                    self.assertEqual(engine.status()["state"], "pending")
                engine.rebuild(published_at=LATER)
                self.assertEqual(engine.status()["state"], "current")
                self.assertEqual(
                    engine.get("runs", self.run["run_id"])["lifecycle"]["state"], "finish_declared"
                )
            PROPERTY_OBSERVATIONS.append({"stage": stage, "signal": "SIGKILL", "pass": True})

    def test_selection_missing_superseded_parent_and_cycle_remain_pending(self):
        second = self.context_for("fork")
        self.engine.import_documents([docrecord(second)])
        candidates = [self.context["attempt_id"], second["attempt_id"]]
        self.ingest(
            self.process()
            + self.process(second)
            + [self.choose(candidates[0], candidates, name="s2", supersedes="s1")]
        )
        self.rebuild()
        self.assertEqual(self.instance()["selection"]["state"], "selection_chain_unresolved")
        self.ingest([self.choose(candidates[1], candidates, name="s1", supersedes="s2")])
        self.rebuild()
        self.assertEqual(self.instance()["selection"]["state"], "selection_chain_unresolved")
        self.assertIsNone(self.instance()["current_attempt_id"])

    def test_unknown_finish_coverage_and_incomparable_declarations_never_imply_completion(self):
        unknown = "att_" + "f" * 64
        self.ingest(self.process() + [self.finish([self.context["attempt_id"], unknown])])
        self.rebuild()
        self.assertEqual(self.run_projection()["lifecycle"]["state"], "pending_association")
        self.assertEqual(len(self.run_projection()["attempt_ids"]), 1)
        self.ingest(
            [self.finish([self.context["attempt_id"]], declaration="smaller", result="abandoned")]
        )
        self.rebuild()
        # Coverage inclusion, rather than timestamp, keeps the wider declaration pending.
        self.assertEqual(self.run_projection()["lifecycle"]["state"], "pending_association")
        self.ingest([self.finish(["att_" + "e" * 64], declaration="incomparable")])
        self.rebuild()
        self.assertEqual(self.run_projection()["lifecycle"]["state"], "conflict")

    def test_context_with_valid_hash_but_wrong_parameter_type_stays_unassociated(self):
        bad = self.context_for("wrong-type")
        bad["parameters"]["member"] = 1
        bad["task_instance_id"] = instance_id(bad["run_id"], bad["task_key"], bad["parameters"])
        reseal(bad)
        self.engine.import_documents([docrecord(bad)])
        self.ingest(self.process(bad))
        self.rebuild()
        self.assertIn("parent_mismatch", self.attempt(bad)["observability"]["flags"])
        self.assertEqual(self.instance(bad)["execution"]["state"], "ambiguous")
        bad_run = copy.deepcopy(self.run)
        bad_run["scope"] = [{"task_key": bad["task_key"], "parameters": bad["parameters"]}]
        reseal(bad_run)
        bad_context = copy.deepcopy(bad)
        bad_context["run_context_sha256"] = bad_run["context_sha256"]
        reseal(bad_context)
        with RawStore(self.root / "bad-scope.sqlite") as store:
            engine = ProjectionEngine(store)
            engine.import_documents(
                [
                    docrecord(self.bundle["definition_manifest"]),
                    docrecord(bad_run),
                    docrecord(bad_context),
                ]
            )
            store.ingest([record(e, locator=e["event_id"]) for e in self.process(bad_context)])
            engine.rebuild(published_at=LATER)
            self.assertIn(
                "parent_mismatch",
                engine.get("attempts", bad_context["attempt_id"])["observability"]["flags"],
            )

    def test_multiple_invocations_are_not_paired_with_one_exit(self):
        events = self.process()
        second = self.event(
            "attempt.started",
            {"invocation_id": "another-invocation", "process_role": self.context["process_role"]},
        )
        self.ingest(events + [second])
        self.rebuild()
        self.assertEqual(self.attempt()["execution"]["state"], "conflict")
        self.assertEqual(self.instance()["execution"]["state"], "ambiguous")
        self.assertEqual(self.store.counts().events, 4)

    def test_cross_session_exit_cannot_complete_attempt_or_replace_old_revision(self):
        events = self.process()
        self.ingest(events[:2])
        old = self.rebuild()
        self.assertEqual(self.attempt()["execution"]["state"], "started")
        events[-1]["source"]["session_id"] = "other-session"
        self.ingest(events[2:])
        new = self.rebuild()
        self.assertEqual(self.attempt()["execution"]["state"], "conflict")
        self.assertEqual(self.instance()["execution"]["state"], "ambiguous")
        self.assertNotEqual(old.revision_id, new.revision_id)
        self.assertEqual(self.engine.revision(old.revision_id), old)

    def test_previous_rule_version_revision_remains_addressable(self):
        self.ingest(self.process())
        with patch(
            "sledra_analysis.projections.engine.REDUCER_VERSION", "sledra-state-reducer-1.0.0"
        ):
            with patch.object(self.engine, "rule_version", "sledra-state-rules-1.0.0"):
                old = self.rebuild()
        new = self.rebuild()
        self.assertNotEqual(old.revision_id, new.revision_id)
        self.assertEqual(self.engine.revision(old.revision_id), old)
        self.assertEqual(self.engine.revision(), new)

    def test_document_import_storage_failure_rolls_back_sources_and_work(self):
        before = self.store.projection_work()

        def fail(name):
            if name == "after_document_import":
                raise OSError("injected document write failure")

        with patch.object(self.engine, "_stage", side_effect=fail), self.assertRaises(OSError):
            self.engine.import_documents([docrecord(self.bundle["job_ref"])])
        self.assertEqual(self.store.projection_work(), before)
        self.assertEqual(
            self.store._db()
            .execute("SELECT count(*) FROM projection_document_sources WHERE kind='job_ref'")
            .fetchone()[0],
            0,
        )
        self.assertEqual(self.engine.import_documents([docrecord(self.bundle["job_ref"])]), 1)

    def test_product_observations_keep_artifact_version_and_rule_without_promotion(self):
        events = []
        for version, rule, result in [("av_A", "1", "pass"), ("av_B", "2", "fail")]:
            events.append(
                self.event(
                    "artifact.validation_completed",
                    {
                        "artifact_version_ref": version,
                        "rule_id": "check",
                        "rule_version": rule,
                        "check_id": "check-" + version,
                        "result": result,
                    },
                )
            )
        self.ingest(events)
        self.rebuild()
        observations = self.run_projection()["product_observations"]
        self.assertEqual(
            {(o["artifact_version_ref"], o["rule_version"], o["result"]) for o in observations},
            {("av_A", "1", "pass"), ("av_B", "2", "fail")},
        )
        self.assertEqual(self.run_projection()["product_state"]["state"], "unknown")
        self.assertFalse(any(o["event_type"] == "delivery.confirmed" for o in observations))

    def test_corrupted_persisted_event_or_parent_digest_blocks_reconstruction(self):
        self.ingest(self.process())
        old = self.rebuild()
        for table in ("event_versions", "projection_documents"):
            row = (
                self.store._db()
                .execute(f"SELECT rowid,canonical_bytes FROM {table} LIMIT 1")
                .fetchone()
            )
            self.store._db().execute(
                f"UPDATE {table} SET canonical_bytes=? WHERE rowid=?", (b"{}", row[0])
            )
            with self.assertRaises(ProjectionError):
                self.rebuild()
            self.assertEqual(self.engine.revision(), old)
            self.store._db().execute(
                f"UPDATE {table} SET canonical_bytes=? WHERE rowid=?", (row[1], row[0])
            )
        self.assertEqual(self.rebuild(), old)


if __name__ == "__main__":
    unittest.main()
