"""SLD-23 core: real process death across the entire frozen offline pipeline."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flow import CUTS, ROOT, STAMP, pipeline, prepare, save, summary

from sledra_analysis.ingest.source import SourceError
from sledra_analysis.ingest.transaction import AtomicIngestor
from sledra_analysis.metrics.engine import MetricsEngine
from sledra_analysis.projections.engine import ProjectionEngine
from sledra_analysis.store.raw import RawStore, StoreError
from sledra_analysis.store.recovery import RecoveryStore

ARTIFACT_ROOT = None
BINARY = None
SPEC = None
OBSERVATIONS = []


class CoreFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.owned = ARTIFACT_ROOT is None
        cls.base = (
            Path(tempfile.mkdtemp(prefix="sledra core fault ")) if cls.owned else ARTIFACT_ROOT
        )
        cls.base.mkdir(mode=0o700, parents=True, exist_ok=True)
        cls.failed = False
        prepare(cls.base / "baseline")
        cls.expected = pipeline(cls.base / "baseline")

    @classmethod
    def tearDownClass(cls):
        if cls.owned and not cls.failed:
            shutil.rmtree(cls.base)
        elif cls.failed:
            print(f"Conformance work retained: {cls.base}", file=sys.stderr)

    def setUp(self):
        self.root = self.base / self._testMethodName
        self.manifest = prepare(self.root)

    def tearDown(self):
        result = self._outcome.result
        if result and any(t is self for t, _ in result.failures + result.errors):
            type(self).failed = True

    def assert_equivalent(self, actual):
        expected = self.expected
        missing = sorted(set(expected["versions"]) - set(actual["versions"]))
        extra = sorted(set(actual["versions"]) - set(expected["versions"]))
        diff = {
            "missing": [
                {"fact": f, "locations": self.manifest["expected_versions"][f]} for f in missing
            ],
            "extra": extra,
            "input_manifest": str(self.root / "inputs.json"),
        }
        save(self.root / "event-diff.json", diff)
        self.assertEqual(missing, [], diff)
        self.assertEqual(extra, [], diff)
        for key in (
            "counts",
            "quarantine_count",
            "projection_id",
            "projection_data",
            "timing_id",
            "timing_data",
            "checkpoint_offsets",
        ):
            self.assertEqual(actual[key], expected[key], f"{key}: {self.root / 'event-diff.json'}")
        self.assertEqual(actual["projection_status"]["state"], "current")
        self.assertEqual(actual["timing_status"]["state"], "current")

    def test_frozen_pipeline_resolves_history_without_starting_business(self):
        actual = pipeline(self.root)
        self.assert_equivalent(actual)
        self.assertEqual(actual["counts"]["events"], 19)
        self.assertEqual(len(actual["projection_data"]["runs"]), 2)
        self.assertEqual(len(actual["projection_data"]["attempts"]), 4)
        late = json.loads((self.root / "shared/contexts/late-attempt.json").read_bytes())
        run = next(r for r in actual["projection_data"]["runs"] if r["id"] == late["run_id"])
        self.assertEqual(run["lifecycle"]["state"], "needs_finalization")
        self.assertTrue(
            all(
                row["duration_ns"] is None
                for row in actual["timing_data"]["intervals"]
                if row["metric"] == "process_execution"
            )
        )

    def test_seeded_duplicates_and_source_permutations_rebuild_same_ids(self):
        actual = pipeline(self.root, seed=23, duplicate=True)
        self.assert_equivalent(actual)
        OBSERVATIONS.append(
            {"scenario": "source_permutation", "seed": 23, "duplicate": True, "pass": True}
        )

    def test_acknowledged_source_loss_restore_and_empty_database_rebuild(self):
        actual = pipeline(self.root)
        recovery = RecoveryStore(self.root / "recovery")
        for receipt in actual["receipts"]:
            recovery.verify_receipt(receipt)
        # Deletion is confined to freshly copied, acknowledged private fixture segments.
        for name in self.manifest["segments"]:
            (self.root / "shared" / name).unlink()
        recovery.restore(actual["backup"], self.root / "restored.sqlite")
        with RawStore(self.root / "restored.sqlite") as store:
            self.assert_equivalent(summary(store))
        rebuilt = self.root / "from-archives"
        prepare(rebuilt)
        for name, receipt in zip(self.manifest["segments"], actual["receipts"], strict=True):
            raw = (recovery.root / receipt["raw_archive"]).read_bytes()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), receipt["raw_hash"])
            (rebuilt / "shared" / name).write_bytes(raw)
        self.assert_equivalent(pipeline(rebuilt, seed=23))
        OBSERVATIONS.append(
            {
                "scenario": "verified_ack_source_removal",
                "segments": len(actual["receipts"]),
                "restore_and_raw_rebuild": True,
            }
        )

    def test_backup_before_all_receipts_is_rejected_without_destination(self):
        actual = pipeline(self.root)
        recovery = RecoveryStore(self.root / "recovery")
        # The first receipt's snapshot predates the remaining source archives.
        with self.assertRaises(StoreError):
            recovery.restore(actual["receipts"][0]["backup"], self.root / "unsafe.sqlite")
        self.assertFalse((self.root / "unsafe.sqlite").exists())

    def test_corrupt_archive_blocks_receipt_and_restore(self):
        actual = pipeline(self.root)
        recovery = RecoveryStore(self.root / "recovery")
        receipt = actual["receipts"][0]
        (recovery.root / receipt["raw_archive"]).write_bytes(b"corrupt synthetic archive\n")
        with self.assertRaises(StoreError):
            recovery.verify_receipt(receipt)
        with self.assertRaises(StoreError):
            recovery.restore(actual["backup"], self.root / "unsafe.sqlite")
        self.assertFalse((self.root / "unsafe.sqlite").exists())

    def test_projection_and_timing_can_lag_ingestion_then_converge(self):
        actual = pipeline(self.root)
        with RawStore(self.root / "facts.sqlite") as store:
            projection, metrics = ProjectionEngine(store), MetricsEngine(store)
            with store.transaction():
                store._db().execute("DELETE FROM timing_head")
                store._db().execute("DELETE FROM projection_heads")
            self.assertEqual(projection.status()["state"], "pending")
            self.assertEqual(metrics.status()["state"], "pending")
            projection.rebuild(published_at=STAMP)
            self.assertEqual(metrics.status()["state"], "pending")
            metrics.publish(report_as_of=STAMP, published_at=STAMP)
            self.assert_equivalent(summary(store))
            self.assertEqual(store.counts().events, actual["counts"]["events"])

    def test_partial_tail_quarantine_and_conflicting_event_keep_offsets(self):
        actual = pipeline(self.root)
        first = json.loads(
            (self.root / "shared" / self.manifest["segments"][0]).read_bytes().splitlines()[1]
        )
        changed = copy.deepcopy(first)
        changed["recorded_at"] = "2026-10-15T01:00:00Z"
        from sledra_analysis.store.canonical import canonical_bytes

        header = {
            "record_kind": "segment_header",
            "protocol_version": "1.0.0",
            "source_id": "fault-source",
            "segment_uuid": "00000000-0000-4000-8000-000000000023",
            "writer_session": "fault-session",
            "created_at": STAMP,
        }
        prefix = canonical_bytes(header) + b"\n" + canonical_bytes(changed) + b"\n"
        raw = prefix + b"\xff\npartial"
        path = self.root / "shared/fault.open.jsonl"
        path.write_bytes(raw)
        with RawStore(self.root / "facts.sqlite") as store:
            ingest = AtomicIngestor(store, path.parent, "sld-23-private", chunk_size=1)
            result = ingest.scan(path.name, collected_at=STAMP)
            self.assertEqual(result.checkpoint.committed_offset, len(prefix) + 2)
            self.assertEqual(store.quarantine_bytes(result.quarantine_ids[0]), b"\xff\n")
            self.assertEqual(store.counts().events, 19)
            self.assertEqual(store.counts().versions, 20)
            self.assertEqual(store.counts().conflicts, 1)
            self.assertEqual(ProjectionEngine(store).status()["state"], "pending")
            state = ProjectionEngine(store).rebuild(published_at=STAMP)
            timing = MetricsEngine(store).publish(report_as_of=STAMP, published_at=STAMP)
            self.assertNotEqual(state.revision_id, actual["projection_id"])
            self.assertNotEqual(timing.report_id, actual["timing_id"])
            with self.assertRaises(SourceError):
                RecoveryStore(self.root / "recovery").acknowledge(
                    store, path.parent, "sld-23-private", path.name, acknowledged_at=STAMP
                )
            locations = {
                "locator": path.name,
                "bad_range": [len(prefix), len(prefix) + 2],
                "pending_range": [len(prefix) + 2, len(raw)],
                "event_id": changed["event_id"],
            }
            save(self.root / "fault-locations.json", locations)
            self.assertEqual(store.events(conflicts_only=True)[0].event_id, changed["event_id"])

    def test_failed_backup_after_projection_and_report_never_acknowledges(self):
        # Kill after metrics commit gives a fully derived but still unacknowledged DB.
        process = subprocess.run(
            [
                sys.executable,
                str(ROOT / "tests/conformance/flow.py"),
                str(self.root),
                "--cut",
                "metrics:after_commit",
            ],
            capture_output=True,
            timeout=40,
        )
        self.assertEqual(process.returncode, -signal.SIGKILL, process.stderr.decode())
        with RawStore(self.root / "facts.sqlite") as store:
            recovery = RecoveryStore(self.root / "recovery")
            with patch.object(
                recovery, "backup", side_effect=OSError("synthetic disk unavailable")
            ):
                with self.assertRaises(OSError):
                    recovery.acknowledge(
                        store,
                        self.root / "shared",
                        "sld-23-private",
                        self.manifest["segments"][0],
                        acknowledged_at=STAMP,
                    )
            self.assertEqual(list((recovery.root / "acknowledgements").glob("*.json")), [])
        self.assert_equivalent(pipeline(self.root))

    def test_live_shell_owner_records_offline_without_changing_payload(self):
        # This is the only live business fixture: the repository's synthetic
        # payload, run exactly once per enabled/disabled comparison, no scheduler.
        sys.path.insert(0, str(ROOT))
        from scripts.test_session import clean_env
        from scripts.test_tools import binary_for

        binary = BINARY if BINARY is not None else binary_for("sledra", self.root)
        comparisons = []
        for mode in ("success", "fail"):
            observations = []
            for enabled in (False, True):
                work = self.root / f"owner-{mode}-{enabled}"
                work.mkdir()
                (work / "observability").mkdir(mode=0o700)
                env = clean_env() | {
                    "STANDALONE_WORK_DIR": str(work),
                    "STANDALONE_PAYLOAD_MODE": mode,
                    "SLEDRA_BIN": str(binary),
                    "SLEDRA_ENABLED": "1" if enabled else "0",
                    "SLEDRA_ROOT": str(work / "observability"),
                    "SLEDRA_SPEC_DIR": str(SPEC or ROOT / "spec/1.0.0"),
                }
                completed = subprocess.run(
                    [str(ROOT / "examples/standalone/run.sh")],
                    env=env,
                    capture_output=True,
                    timeout=30,
                )
                business = {
                    "rc": completed.returncode,
                    "stdout_hex": completed.stdout.hex(),
                    "stderr_hex": completed.stderr.hex(),
                    "executions": (work / "executions.log").read_text(),
                    "callbacks": (work / "callbacks.log").read_text(),
                    "product": (work / "product.txt").read_bytes().hex()
                    if (work / "product.txt").exists()
                    else None,
                }
                observations.append(business)
                self.assertEqual(business["executions"], "run\n")
                self.assertFalse((work / "facts.sqlite").exists())
                if not enabled:
                    self.assertFalse(list((work / "observability").rglob("*.jsonl")))
                    continue
                with RawStore(work / "facts.sqlite") as store:
                    projection = ProjectionEngine(store)
                    shared = work / "observability"
                    ingest = AtomicIngestor(store, shared, "sld-23-live-owner")
                    for path in sorted(shared.rglob("*.json")):
                        value = json.loads(path.read_bytes())
                        if "outbox_event" in value:
                            ingest.registry(
                                str(path.relative_to(shared)),
                                source_id=value["outbox_event"]["source"]["source_id"],
                                collected_at=STAMP,
                            )
                        elif value.get("kind") == "definition_manifest":
                            from sledra_analysis.projections.documents import DocumentRecord

                            projection.import_documents(
                                [
                                    DocumentRecord(
                                        path.read_bytes(), str(path.relative_to(shared)), STAMP
                                    )
                                ]
                            )
                    for path in sorted(shared.rglob("*.jsonl")):
                        ingest.scan(str(path.relative_to(shared)), collected_at=STAMP)
                    state = projection.rebuild(published_at=STAMP)
                    self.assertEqual(len(state.data["runs"]), 1)
                    self.assertEqual(len(state.data["attempts"]), 1)
                    self.assertEqual(
                        state.data["attempts"][0]["execution"]["exit_code"], completed.returncode
                    )
                    self.assertFalse(
                        any(e["event_type"] == "delivery.confirmed" for e in state.data["facts"])
                    )
                    metrics = MetricsEngine(store)
                    report = metrics.publish(report_as_of=STAMP, published_at=STAMP)
                    self.assertTrue(report.data["intervals"])
                    save(
                        work / "offline-result.json",
                        {
                            "projection_id": state.revision_id,
                            "timing_id": report.report_id,
                            "counts": {"runs": 1, "attempts": 1},
                            "business": business,
                        },
                    )
            self.assertEqual(observations[0], observations[1])
            comparisons.append(
                {
                    "mode": mode,
                    "business_equal": True,
                    "executions_per_case": 1,
                    "callbacks": observations[0]["callbacks"],
                    "rc": observations[0]["rc"],
                }
            )
        save(self.root / "business-diff.json", comparisons)
        OBSERVATIONS.append(
            {
                "scenario": "live_owner_offline_analysis",
                "comparisons": comparisons,
                "scheduler_submissions": 0,
            }
        )

    def test_mock_accept_local_commit_failure_and_payload_before_acceptance(self):
        from sledra_analysis.projections.documents import DocumentRecord
        from sledra_analysis.store.canonical import canonical_bytes

        bundle = json.loads((ROOT / "spec/fixtures/1.0.0/valid-bundle.json").read_bytes())
        catalog = json.loads((ROOT / "spec/1.0.0/event-types.json").read_bytes())
        c, job = bundle["attempt_context"], bundle["job_ref"]

        def event(name, payload, serial, with_job=False):
            value = copy.deepcopy(bundle["events"][0])
            value.update(
                event_id=f"00000000-0000-4000-8000-{serial:012x}",
                event_type=name,
                payload=payload,
                association={k: c[k] for k in ("run_id", "task_instance_id", "attempt_id")},
                source={
                    "source_id": "mock-submit",
                    "session_id": "submit-a",
                    "kind": catalog[name]["source_kinds"][0],
                },
            )
            value["producer"]["role"] = catalog[name]["roles"][0]
            if with_job:
                value["association"]["job_ref_id"] = job["job_ref_id"]
            return value

        requested = event(
            "submission.requested",
            {
                "submission_instance": "submit-a",
                "platform_id": job["platform_id"],
                "scheduler": job["scheduler"],
                "resource_ref": "mock-only",
            },
            1,
        )
        started = event(
            "attempt.started",
            {"invocation_id": "mock-invocation", "process_role": c["process_role"]},
            2,
        )
        accepted = event(
            "submission.accepted",
            {
                "submission_instance": "submit-a",
                "job_ref_id": job["job_ref_id"],
                "confirmation_basis": "mock-accepted-once",
            },
            3,
            True,
        )
        # The mock's acceptance count is external to the local observation store;
        # re-reading its durable evidence must never invoke this business action.
        mock_calls = []

        def submit_once():
            mock_calls.append("accepted")
            return accepted

        acknowledgement = submit_once()
        with RawStore(self.root / "mock.sqlite") as store:
            projection = ProjectionEngine(store)
            projection.import_documents(
                [
                    DocumentRecord(canonical_bytes(bundle[k]), k + ".json", STAMP)
                    for k in ("definition_manifest", "run_context", "attempt_context")
                ]
            )
            shared = self.root / "mock-shared"
            shared.mkdir()
            header = {
                "record_kind": "segment_header",
                "protocol_version": "1.0.0",
                "source_id": "mock-submit",
                "segment_uuid": "00000000-0000-4000-8000-000000002323",
                "writer_session": "submit-a",
                "created_at": STAMP,
            }
            path = shared / "mock.open.jsonl"
            path.write_bytes(
                canonical_bytes(header)
                + b"\n"
                + canonical_bytes(requested)
                + b"\n"
                + canonical_bytes(started)
                + b"\n"
            )
            ingest = AtomicIngestor(store, shared, "mock-submission")
            ingest.scan(path.name, collected_at=STAMP)
            early = projection.rebuild(published_at=STAMP)
            attempt = early.data["attempts"][0]
            self.assertEqual(attempt["execution"]["state"], "started")
            self.assertEqual(attempt["job_ref_ids"], [])
            with path.open("ab") as stream:
                stream.write(canonical_bytes(acknowledgement) + b"\n")

            def fail(name):
                if name == "after_records":
                    raise OSError("synthetic accepted-event local commit failure")

            with patch.object(ingest, "_stage", fail):
                with self.assertRaises(OSError):
                    ingest.scan(path.name, collected_at=STAMP)
            self.assertEqual(store.counts().events, 2)
            projection.import_documents(
                [DocumentRecord(canonical_bytes(job), "job_ref.json", STAMP)]
            )
            ingest.scan(path.name, collected_at=STAMP)
            ingest.scan(path.name, collected_at=STAMP)
            final = projection.rebuild(published_at=STAMP)
            self.assertEqual(final.data["attempts"][0]["job_ref_ids"], [job["job_ref_id"]])
            self.assertEqual(final.data["attempts"][0]["execution"]["state"], "started")
            self.assertEqual(store.counts().events, 3)
            self.assertEqual(mock_calls, ["accepted"])
            save(
                self.root / "mock-submit-result.json",
                {
                    "mock_submissions": len(mock_calls),
                    "payload_started_before_local_acceptance": True,
                    "local_commit_failure_replayed": True,
                    "versions": [e.event_id for e in store.events()],
                    "source": path.name,
                    "acknowledgement_byte_end": path.stat().st_size,
                },
            )

    def crash(self, cut):
        process = subprocess.run(
            [sys.executable, str(ROOT / "tests/conformance/flow.py"), str(self.root), "--cut", cut],
            capture_output=True,
            timeout=40,
        )
        self.assertEqual(
            process.returncode,
            -signal.SIGKILL,
            f"{cut}: {process.stderr.decode()}; {self.root / 'inputs.json'}",
        )
        self.assertEqual(
            json.loads((self.root / "killed-at.json").read_bytes())["component"], cut.split(":")[0]
        )
        # Both old/missing and new committed heads are valid here; validate their
        # referential consistency before replay instead of silently repairing first.
        with RawStore(self.root / "facts.sqlite") as store:
            self.assertEqual(store._db().execute("PRAGMA quick_check").fetchall(), [("ok",)])
            self.assertEqual(store._db().execute("PRAGMA foreign_key_check").fetchall(), [])
            if cut in ("ingest:after_records", "ingest:after_checkpoint", "ingest:before_commit"):
                self.assertEqual(store.counts().events, 0)
                self.assertEqual(store.checkpoints("sld-23-private"), ())
            if cut.startswith("recovery:"):
                recovery = RecoveryStore(self.root / "recovery")
                for path in (recovery.root / "acknowledgements").glob("*.json"):
                    recovery.verify_receipt(json.loads(path.read_bytes()))
        resumed = pipeline(self.root, seed=23, duplicate=True)
        self.assert_equivalent(resumed)
        recovery = RecoveryStore(self.root / "recovery")
        recovery.restore(resumed["backup"], self.root / "restored.sqlite")
        with RawStore(self.root / "restored.sqlite") as store:
            self.assert_equivalent(summary(store))
        OBSERVATIONS.append(
            {
                "scenario": "end_to_end_sigkill",
                "cut": cut,
                "pass": True,
                "expected_events": 19,
                "versions_equal": True,
                "projection_equal": True,
                "timing_equal": True,
                "input_manifest": str(self.root / "inputs.json"),
                "event_diff": str(self.root / "event-diff.json"),
            }
        )


for _cut in CUTS:

    def _test(self, cut=_cut):
        self.crash(cut)

    setattr(CoreFlowTests, "test_crash_" + _cut.replace(":", "_"), _test)


if __name__ == "__main__":
    unittest.main(verbosity=2)
