"""V12/V21: atomic boundaries, process death, durable receipts and recovery."""

from __future__ import annotations

import copy
import errno
import io
import json
import signal
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from store_fixture import frozen_records
from test_reader import line
from test_source import segment, UUID_B
from test_store import COLLECTED, LATER, event, record

from sledra_analysis.ingest.source import SourceChanged, SourceError, SourceTracker
from sledra_analysis.ingest.validation import EventValidator
from sledra_analysis.ingest.transaction import AtomicIngestor
from sledra_analysis.store.canonical import canonical_bytes
from sledra_analysis.store.raw import RawStore, StoreCounts, StoreError
from sledra_analysis.store.recovery import RecoveryStore

CRASH_OBSERVATIONS = []


def signature(store):
    return [
        (e.event_id, e.conflicted, [(v.canonical_hash, v.canonical_bytes) for v in e.versions])
        for e in store.events()
    ]


def drop_v3(connection):
    for name in ("enqueue_event", "enqueue_validation", "enqueue_quarantine"):
        connection.execute(f"DROP TRIGGER {name}")
    for name in (
        "raw_archives",
        "registry_checkpoints",
        "checkpoints",
        "projection_work_queue",
        "ingestion_commits",
        "ingestion_identity",
    ):
        connection.execute(f"DROP TABLE {name}")
    connection.execute("PRAGMA user_version=2")


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="sledra V12 V21 private ")
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.shared = self.root / "shared"
        self.shared.mkdir(mode=0o700)
        self.dbpath = self.root / "facts.sqlite"
        self.store = RawStore(self.dbpath)
        self.addCleanup(lambda: self.store.close())
        self.ingestor = AtomicIngestor(self.store, self.shared, "test-root", chunk_size=7)
        self.recovery = RecoveryStore(self.root / "recovery")

    def write(self, body=None, name="a.sealed.jsonl", uuid=None):
        raw = segment(line() if body is None else body, **({"uuid": uuid} if uuid else {}))
        path = self.shared / name
        path.write_bytes(raw)
        return path

    def scan(self, path, **kwargs):
        return self.ingestor.scan(path.name, collected_at=COLLECTED, **kwargs)

    def reopen(self):
        self.store.close()
        self.store = RawStore(self.dbpath)
        self.ingestor = AtomicIngestor(self.store, self.shared, "test-root", chunk_size=7)

    def ack(self, path):
        return self.recovery.acknowledge(
            self.store, self.shared, "test-root", path.name, acknowledged_at=LATER
        )

    def assert_empty(self):
        self.assertEqual(self.store.counts(), StoreCounts(0, 0, 0, 0))
        self.assertEqual(self.store.quarantines(), ())
        self.assertEqual(self.store.checkpoints("test-root"), ())
        self.assertEqual(self.store.projection_work(), ())
        self.assertEqual(self.store.watermark()["commit_id"], 0)

    def test_atomic_batch_restart_retains_conflicts_bad_bytes_and_opaque_queue(self):
        path = self.write(
            line()
            + b"\xff\n"
            + line(recorded_at=LATER)
            + line(2, event_type="future.critical", payload={}, schema_version="1.9.0")
        )
        result = self.scan(path)
        self.assertEqual(result.checkpoint.committed_offset, path.stat().st_size)
        self.assertEqual(self.store.counts(), StoreCounts(2, 3, 3, 1))
        self.assertEqual(self.store.quarantine_bytes(result.quarantine_ids[0]), b"\xff\n")
        self.assertEqual(len(self.store.projection_work()), 8)
        expected, work = signature(self.store), self.store.projection_work()
        self.reopen()
        self.assertEqual(self.scan(path).records, 0)
        self.assertEqual(signature(self.store), expected)
        self.assertEqual(self.store.projection_work(), work)
        self.assertEqual(self.store._db().execute("PRAGMA foreign_key_check").fetchall(), [])

    def test_precommit_exceptions_roll_back_records_queue_and_checkpoint(self):
        path = self.write(line() + b"{bad}\n" + line(2))
        for stage in ("after_records", "after_checkpoint", "before_commit"):
            with self.subTest(stage=stage):

                def fail(name):
                    if name == stage:
                        raise OSError(errno.EIO, "injected I/O failure")

                with (
                    patch.object(self.ingestor, "_stage", side_effect=fail),
                    self.assertRaises(OSError),
                ):
                    self.scan(path)
                self.assert_empty()
        self.scan(path)
        self.assertEqual(self.store.counts().events, 2)

    def test_postcommit_exception_recovers_without_duplicate_facts_or_queue(self):
        path = self.write()

        def fail(name):
            if name == "after_commit":
                raise OSError("lost commit response")

        with patch.object(self.ingestor, "_stage", side_effect=fail), self.assertRaises(OSError):
            self.scan(path)
        queue = self.store.projection_work()
        self.reopen()
        self.assertEqual(self.scan(path).records, 0)
        self.assertEqual(self.store.counts().raw_records, 1)
        self.assertEqual(self.store.projection_work(), queue)

    def test_sqlite_commit_busy_rolls_back_entire_batch(self):
        path = self.write(line() + b"bad\n")
        reader = sqlite3.connect(self.dbpath, isolation_level=None)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM events").fetchall()
        with self.assertRaises(sqlite3.OperationalError):
            self.scan(path)
        self.assert_empty()
        reader.execute("ROLLBACK")
        self.scan(path)
        self.assertEqual(len(self.store.quarantines()), 1)

    def test_sqlite_full_rolls_back_good_fact_and_oversized_isolation(self):
        path = self.write(line() + b"x" * (2 * 1024 * 1024) + b"\n")
        pages = self.store._db().execute("PRAGMA page_count").fetchone()[0]
        self.store._db().execute(f"PRAGMA max_page_count={pages + 8}")
        with self.assertRaises(sqlite3.OperationalError) as caught:
            self.scan(path)
        self.assertEqual(caught.exception.sqlite_errorcode, sqlite3.SQLITE_FULL)
        self.assert_empty()
        self.store._db().execute("PRAGMA max_page_count=1073741823")
        self.scan(path)
        self.assertEqual(self.store.counts().events, 1)
        self.assertEqual(len(self.store.quarantines()), 1)

    def test_queue_quarantine_checkpoint_failures_roll_back_other_tables(self):
        path = self.write(line() + b"bad\n")
        for table in ("projection_work_queue", "quarantine", "checkpoints", "raw_records"):
            with self.subTest(table=table):
                self.store._db().execute(
                    f"CREATE TEMP TRIGGER fail_write BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT, 'injected'); END"
                )
                with self.assertRaises(sqlite3.IntegrityError):
                    self.scan(path)
                self.store._db().execute("DROP TRIGGER fail_write")
                self.assert_empty()
        self.scan(path)

    def test_source_mutation_before_commit_rolls_back_everything(self):
        path = self.write()

        def mutate(name):
            if name == "after_checkpoint":
                path.write_bytes(segment(line(2)))

        with (
            patch.object(self.ingestor, "_stage", side_effect=mutate),
            self.assertRaises(SourceChanged),
        ):
            self.scan(path)
        self.assert_empty()
        self.scan(path)
        self.assertEqual(self.store.counts().events, 1)

    def test_checkpoint_rejects_missing_raw_and_quarantine_coverage(self):
        path = self.write(line() + line(2))
        with SourceTracker(self.shared, "test-root").open(path.name) as lease:
            state = lease.checkpoint(path.stat().st_size)
        with self.assertRaises(StoreError), self.store._ingestion_transaction(COLLECTED) as commit:
            self.store._save_checkpoint(state, (), commit)
        self.assert_empty()

    def test_partial_record_and_quota_resume_from_durable_line_boundary(self):
        path = self.write(line() + line(2)[:-1], "a.open.jsonl")
        first = self.scan(path)
        self.assertLess(first.checkpoint.committed_offset, path.stat().st_size)
        self.reopen()
        with path.open("ab") as stream:
            stream.write(b"\n" + line(3))
        self.ingestor = AtomicIngestor(self.store, self.shared, "test-root", max_records=1)
        second = self.scan(path)
        self.assertEqual(second.records, 1)
        self.reopen()
        self.assertEqual(self.scan(path).records, 1)
        self.assertEqual(self.store.counts().events, 3)

    def test_sealed_residual_and_empty_segment_can_ack_only_after_ingestion(self):
        path = self.write(line() + b"broken")
        with self.assertRaises(SourceError):
            self.ack(path)
        result = self.scan(path)
        self.assertEqual(self.store.quarantines()[0].reason, "sealed_incomplete_tail")
        receipt = self.ack(path)
        self.assertEqual(receipt["committed_offset"], result.checkpoint.sealed_size)
        self.recovery.verify_receipt(receipt)
        empty = self.write(b"", "empty.sealed.jsonl", uuid=UUID_B)
        self.scan(empty)
        self.ack(empty)

    def test_open_or_incompletely_consumed_segment_cannot_ack(self):
        path = self.write(line() + line(2), "a.open.jsonl")
        self.scan(path)
        with self.assertRaises(SourceError):
            self.ack(path)
        sealed = path.with_name("a.sealed.jsonl")
        path.rename(sealed)
        # The open checkpoint has not yet committed its sealed digest.
        with self.assertRaises(StoreError):
            self.ack(sealed)
        self.scan(sealed)
        self.ack(sealed)

    def test_uuid_conflict_stays_blocked_after_restart_and_cannot_ack(self):
        first = self.write()
        self.scan(first)
        second = self.write(line(2), "other.sealed.jsonl")
        with self.assertRaises(SourceError):
            self.scan(second)
        self.reopen()
        self.assertTrue(all(c.conflict for c in self.store.checkpoints("test-root")))
        for path in (first, second):
            with self.assertRaises(SourceError):
                self.scan(path)
            with self.assertRaises(SourceError):
                self.ack(path)
        self.assertEqual(self.store.counts().events, 1)

    def test_legacy_generation_and_integrity_diagnostics_survive_restart(self):
        path = self.shared / "legacy.jsonl"
        path.write_bytes(line() + line(2))
        first = self.scan(path, legacy_source_id="legacy")
        self.assertIn("legacy_source_integrity_unproven", first.diagnostics)
        path.write_bytes(line(3))
        self.reopen()
        second = self.scan(path, legacy_source_id="legacy")
        self.assertEqual(second.checkpoint.generation, 1)
        self.assertIn("source_gap", second.diagnostics)
        self.assertEqual(len(self.store.checkpoints("test-root")), 2)
        self.assertEqual(self.store.counts().events, 3)

    def test_registry_whole_json_atomic_conflicting_versions_and_digest_checkpoint(self):
        path = self.shared / "run.json"
        path.write_bytes(canonical_bytes({"object": "retained", "outbox_event": event()}))
        first = self.ingestor.registry(path.name, source_id="registry", collected_at=COLLECTED)
        self.assertFalse(first.duplicate)
        self.reopen()
        self.assertTrue(
            self.ingestor.registry(path.name, source_id="registry", collected_at=LATER).duplicate
        )
        changed = event()
        changed["recorded_at"] = LATER
        path.write_bytes(canonical_bytes({"outbox_event": changed}))
        second = self.ingestor.registry(path.name, source_id="registry", collected_at=LATER)
        self.assertIn("registry_immutable_conflict", second.diagnostics)
        self.assertEqual(self.store.counts(), StoreCounts(1, 2, 2, 1))
        self.assertEqual(
            self.store._db().execute("SELECT count(*) FROM registry_checkpoints").fetchone()[0], 2
        )
        self.assertNotEqual(first.raw_hash, second.raw_hash)

    def test_registry_bad_and_oversized_json_are_preserved_as_whole_files(self):
        path = self.shared / "bad.json"
        for raw in (b"{bad}", b"x" * (1024 * 1024 + 1)):
            path.write_bytes(raw)
            result = self.ingestor.registry(path.name, source_id="registry", collected_at=COLLECTED)
            stored = b"".join(self.store.quarantine_chunks(result.quarantine_id))
            self.assertEqual(stored, raw)
        self.assertEqual(self.store.counts().events, 0)
        self.assertEqual(len(self.store.quarantines()), 2)

    def test_registry_precommit_failure_or_mutation_leaves_no_digest_checkpoint(self):
        path = self.shared / "run.json"
        path.write_bytes(canonical_bytes({"outbox_event": event()}))
        for stage in ("after_records", "after_checkpoint", "before_commit"):

            def fail(name):
                if name == stage:
                    raise OSError("fail")

            with (
                patch.object(self.ingestor, "_stage", side_effect=fail),
                self.assertRaises(OSError),
            ):
                self.ingestor.registry(path.name, source_id="registry", collected_at=COLLECTED)
            self.assert_empty()
            self.assertEqual(
                self.store._db().execute("SELECT count(*) FROM registry_checkpoints").fetchone()[0],
                0,
            )

        def mutate(name):
            if name == "after_checkpoint":
                path.write_bytes(b"bad")

        with (
            patch.object(self.ingestor, "_stage", side_effect=mutate),
            self.assertRaises(SourceChanged),
        ):
            self.ingestor.registry(path.name, source_id="registry", collected_at=COLLECTED)
        self.assert_empty()

    def test_schema_two_requires_explicit_atomic_upgrade_and_backfills_queue(self):
        self.store.ingest([record()])
        expected = signature(self.store)
        self.store.close()
        with sqlite3.connect(self.dbpath) as db:
            drop_v3(db)
        before = self.dbpath.read_bytes()
        with self.assertRaises(StoreError):
            RawStore(self.dbpath)
        self.assertEqual(self.dbpath.read_bytes(), before)
        with RawStore(self.dbpath, upgrade=True) as upgraded:
            self.assertEqual(signature(upgraded), expected)
            self.assertEqual(upgraded.projection_work()[0][1], "event")
            self.assertEqual(upgraded._db().execute("PRAGMA user_version").fetchone()[0], 3)

    def test_schema_two_failed_upgrade_rolls_back_schema_queue_and_version(self):
        self.store.ingest([record()])
        self.store.close()
        with sqlite3.connect(self.dbpath) as db:
            drop_v3(db)
        reader = sqlite3.connect(self.dbpath, isolation_level=None)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM events").fetchall()
        with self.assertRaises(sqlite3.OperationalError):
            RawStore(self.dbpath, upgrade=True)
        self.assertEqual(reader.execute("PRAGMA user_version").fetchone()[0], 2)
        self.assertIsNone(
            reader.execute("SELECT name FROM sqlite_master WHERE name='checkpoints'").fetchone()
        )
        reader.execute("ROLLBACK")
        self.store = RawStore(self.dbpath, upgrade=True)
        self.assertEqual(self.store.counts().events, 1)

    def test_ack_backup_restore_after_explicit_synthetic_source_removal(self):
        path = self.write(line() + b"bad\n" + line(recorded_at=LATER))
        self.scan(path)
        expected, queue = signature(self.store), self.store.projection_work()
        receipt = self.ack(path)
        self.recovery.verify_receipt(receipt)
        self.assertEqual(self.ack(path), receipt)
        original = path.read_bytes()
        # Explicit private V21 retention simulation, never a product cleanup API.
        path.unlink()
        restored = self.root / "restored.sqlite"
        self.recovery.restore(receipt["backup"], restored)
        with RawStore(restored) as store:
            self.assertEqual(signature(store), expected)
            self.assertEqual(store.projection_work(), queue)
            self.assertEqual(store.quarantine_bytes(1), b"bad\n")
            archived = self.recovery.root / receipt["raw_archive"]
            self.assertEqual(archived.read_bytes(), original)
            # Restored archive at a new locator still resolves the same segment.
            path.write_bytes(archived.read_bytes())
            self.assertEqual(
                AtomicIngestor(store, self.shared, "test-root")
                .scan(path.name, collected_at=LATER)
                .records,
                0,
            )
        self.assertEqual(restored.stat().st_mode & 0o777, 0o600)

    def test_restore_refuses_older_backup_missing_any_published_ack(self):
        first = self.write()
        self.scan(first)
        first_ack = self.ack(first)
        second = self.write(line(2), "b.sealed.jsonl", uuid=UUID_B)
        self.scan(second)
        second_ack = self.ack(second)
        destination = self.root / "restore.sqlite"
        with self.assertRaises(StoreError):
            self.recovery.restore(first_ack["backup"], destination)
        self.assertFalse(destination.exists())
        self.recovery.restore(second_ack["backup"], destination)
        with RawStore(destination) as restored:
            self.assertEqual(restored.counts().events, 2)

    def test_restore_rejects_other_lineage_corrupt_bytes_and_existing_destination(self):
        path = self.write()
        self.scan(path)
        receipt = self.ack(path)
        destination = self.root / "restore.sqlite"
        manifest = copy.deepcopy(receipt["backup"])
        manifest["watermark"]["commit_id"] += 1
        with self.assertRaises(StoreError):
            self.recovery.restore(manifest, destination)
        with RawStore(self.root / "other.sqlite") as other:
            unrelated = self.recovery.backup(other)
        with self.assertRaises(StoreError):
            self.recovery.restore(unrelated, destination)
        raw = self.recovery.root / receipt["raw_archive"]
        original = raw.read_bytes()
        raw.write_bytes(b"corrupt")
        with self.assertRaises(StoreError):
            self.recovery.restore(receipt["backup"], destination)
        raw.write_bytes(original)
        destination.write_bytes(b"keep")
        with self.assertRaises(StoreError):
            self.recovery.restore(receipt["backup"], destination)
        self.assertEqual(destination.read_bytes(), b"keep")
        backup = self.recovery.root / receipt["backup"]["database"]
        backup.write_bytes(b"corrupt")
        with self.assertRaises(StoreError):
            self.recovery.verify_receipt(receipt)

    def test_archive_or_backup_fsync_failure_never_publishes_ack(self):
        path = self.write()
        self.scan(path)
        with (
            patch(
                "sledra_analysis.store.recovery.os.fsync",
                side_effect=OSError(errno.ENOSPC, "injected full archive volume"),
            ),
            self.assertRaises(OSError),
        ):
            self.ack(path)
        self.assertEqual(list((self.recovery.root / "acknowledgements").iterdir()), [])
        self.assertTrue(path.exists())
        self.ack(path)

    def test_lost_confirmation_retries_and_conflicting_publication_never_overwrite(self):
        path = self.write()
        self.scan(path)

        def lose(name):
            if name == "after_ack":
                raise OSError("lost confirmation response")

        with patch.object(self.recovery, "_stage", side_effect=lose), self.assertRaises(OSError):
            self.ack(path)
        receipt = self.ack(path)
        self.recovery.verify_receipt(receipt)
        file = next((self.recovery.root / "acknowledgements").glob("*.json"))
        original = file.read_bytes()
        changed = copy.deepcopy(receipt)
        changed["raw_hash"] = "f" * 64
        file.write_text(json.dumps(changed))
        with self.assertRaises(StoreError):
            self.ack(path)
        self.assertEqual(json.loads(file.read_text()), changed)
        file.write_bytes(original)

    def test_no_ack_at_every_archive_publication_cut_until_backup_is_durable(self):
        path = self.write()
        self.scan(path)
        for stage in (
            "after_raw_archive",
            "after_archive_commit",
            "after_snapshot",
            "after_backup_manifest",
            "before_ack",
        ):
            with self.subTest(stage=stage):

                def fail(name):
                    if name == stage:
                        raise OSError("publication interrupted")

                with (
                    patch.object(self.recovery, "_stage", side_effect=fail),
                    self.assertRaises(OSError),
                ):
                    self.ack(path)
                self.assertTrue(path.exists())
                self.assertEqual(list((self.recovery.root / "acknowledgements").glob("*.json")), [])
        self.recovery.verify_receipt(self.ack(path))

    def test_backup_refuses_uncommitted_state_and_preserves_queue_quarantine(self):
        path = self.write(line() + b"bad\n")
        self.scan(path)
        with self.store.transaction(), self.assertRaises(StoreError):
            self.recovery.backup(self.store)
        snapshot = self.recovery.backup(self.store)
        target = self.root / "restored.sqlite"
        self.recovery.restore(snapshot, target)
        with RawStore(target) as restored:
            self.assertEqual(restored.projection_work(), self.store.projection_work())
            self.assertEqual(restored.checkpoints("test-root"), self.store.checkpoints("test-root"))
            self.assertEqual(restored.quarantine_bytes(1), b"bad\n")

    def test_recovery_and_registry_reject_symlink_escape(self):
        outside = self.root / "outside"
        outside.write_bytes(b"private")
        (self.shared / "link.json").symlink_to(outside)
        with self.assertRaises(OSError):
            self.ingestor.registry("link.json", source_id="registry", collected_at=COLLECTED)
        bad = self.root / "bad-recovery"
        bad.mkdir()
        (bad / "raw").symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            RecoveryStore(bad)
        self.assertEqual(outside.read_bytes(), b"private")

    def test_frozen_19_fact_sources_replay_through_real_transactions(self):
        _, sources = frozen_records(self.root / "frozen")
        ingestor = AtomicIngestor(self.store, self.root / "frozen", "frozen-root")
        for locator, raw in sources.items():
            if locator.endswith(".jsonl"):
                ingestor.scan(locator, collected_at=COLLECTED)
            elif "outbox_event" in json.loads(raw):
                ingestor.registry(
                    locator,
                    source_id=json.loads(raw)["outbox_event"]["source"]["source_id"],
                    collected_at=COLLECTED,
                )
        self.assertEqual(self.store.counts().events, 19)
        self.assertEqual(self.store.quarantines(), ())
        before = signature(self.store)
        self.reopen()
        self.assertEqual(signature(self.store), before)

    def test_process_sigkill_at_ingestion_and_ack_cuts_matches_baseline_after_restart(self):
        self.store.close()
        stages = (
            "after_records",
            "after_checkpoint",
            "before_commit",
            "after_commit",
            "after_raw_archive",
            "after_archive_commit",
            "after_snapshot",
            "after_backup_manifest",
            "before_ack",
            "after_ack",
        )
        body = line() + b"\xff\n" + line(recorded_at=LATER) + line(2)
        baseline_shared = self.root / "baseline-shared"
        baseline_shared.mkdir()
        (baseline_shared / "a.sealed.jsonl").write_bytes(segment(body))
        with RawStore(self.root / "baseline.sqlite") as baseline:
            AtomicIngestor(baseline, baseline_shared, "test-root").scan(
                "a.sealed.jsonl", collected_at=COLLECTED
            )
            baseline_facts = signature(baseline)
            baseline_queue = baseline.projection_work()
        for stage in stages:
            with self.subTest(stage=stage):
                directory = self.root / stage
                shared = directory / "shared"
                shared.mkdir(parents=True)
                path = shared / "a.sealed.jsonl"
                path.write_bytes(segment(body))
                script = """import os, signal, sys
from pathlib import Path
from sledra_analysis.store.raw import RawStore
from sledra_analysis.store.recovery import RecoveryStore
from sledra_analysis.ingest.transaction import AtomicIngestor
root, stage = Path(sys.argv[1]), sys.argv[2]
def die(name):
    if name == stage: os.kill(os.getpid(), signal.SIGKILL)
with RawStore(root/'facts.sqlite') as store:
    ingest=AtomicIngestor(store, root/'shared', 'test-root')
    ingest._stage=die
    ingest.scan('a.sealed.jsonl', collected_at='2026-10-14T00:00:00Z')
    recovery=RecoveryStore(root/'recovery')
    recovery._stage=die
    recovery.acknowledge(store, root/'shared', 'test-root', 'a.sealed.jsonl', acknowledged_at='2026-10-14T01:00:00Z')
"""
                killed = subprocess.run(
                    [sys.executable, "-c", script, str(directory), stage], capture_output=True
                )
                self.assertEqual(killed.returncode, -signal.SIGKILL, killed.stderr.decode())
                with RawStore(directory / "facts.sqlite") as recovered:
                    before = recovered.counts()
                    if stage in ("after_records", "after_checkpoint", "before_commit"):
                        self.assertEqual(before.events, 0)
                        self.assertEqual(recovered.checkpoints("test-root"), ())
                        self.assertEqual(recovered.projection_work(), ())
                    ingest = AtomicIngestor(recovered, shared, "test-root")
                    ingest.scan(path.name, collected_at=COLLECTED)
                    self.assertEqual(recovered.counts(), StoreCounts(2, 3, 3, 1))
                    self.assertEqual(signature(recovered), baseline_facts)
                    self.assertEqual(recovered.projection_work(), baseline_queue)
                    self.assertEqual(len(recovered.quarantines()), 1)
                    self.assertEqual(len(recovered.projection_work()), 8)
                    recovery = RecoveryStore(directory / "recovery")
                    receipt = recovery.acknowledge(
                        recovered, shared, "test-root", path.name, acknowledged_at=LATER
                    )
                    recovery.verify_receipt(receipt)
                    recovery.restore(receipt["backup"], directory / "restored.sqlite")
                    with RawStore(directory / "restored.sqlite") as restored:
                        self.assertEqual(signature(restored), signature(recovered))
                        self.assertEqual(restored.quarantine_bytes(1), b"\xff\n")
                        self.assertEqual(restored.projection_work(), recovered.projection_work())
                    CRASH_OBSERVATIONS.append(
                        {
                            "stage": stage,
                            "signal": "SIGKILL",
                            "pass": True,
                            "facts": 2,
                            "versions": 3,
                            "conflicts": 1,
                            "quarantines": 1,
                            "queue_items": 8,
                        }
                    )

    def test_sigkill_during_table_writes_rolls_back_entire_transaction(self):
        for table in (
            "raw_blobs",
            "raw_records",
            "quarantine_blobs",
            "projection_work_queue",
            "checkpoints",
        ):
            with self.subTest(table=table):
                root = self.root / table
                shared = root / "shared"
                shared.mkdir(parents=True)
                (shared / "a.sealed.jsonl").write_bytes(segment(line() + b"bad\n"))
                script = """import os, signal, sqlite3, sys
from pathlib import Path
from sledra_analysis.store.raw import RawStore
from sledra_analysis.ingest.transaction import AtomicIngestor
root, table = Path(sys.argv[1]), sys.argv[2]
with RawStore(root/'facts.sqlite') as store:
    def authorize(action, name, *args):
        if action == sqlite3.SQLITE_INSERT and name == table:
            os.kill(os.getpid(), signal.SIGKILL)
        return sqlite3.SQLITE_OK
    store._db().set_authorizer(authorize)
    AtomicIngestor(store, root/'shared', 'test-root').scan('a.sealed.jsonl', collected_at='2026-10-14T00:00:00Z')
"""
                result = subprocess.run(
                    [sys.executable, "-c", script, str(root), table], capture_output=True
                )
                self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr.decode())
                with RawStore(root / "facts.sqlite") as recovered:
                    self.assertEqual(recovered.counts().events, 0)
                    self.assertEqual(recovered.checkpoints("test-root"), ())
                    self.assertEqual(recovered.projection_work(), ())
                    self.assertEqual(recovered.quarantines(), ())
                    AtomicIngestor(recovered, shared, "test-root").scan(
                        "a.sealed.jsonl", collected_at=COLLECTED
                    )
                    self.assertEqual(recovered.counts().events, 1)
                    self.assertEqual(recovered.quarantine_bytes(1), b"bad\n")
                CRASH_OBSERVATIONS.append(
                    {"stage": "insert_" + table, "signal": "SIGKILL", "pass": True}
                )

    def test_registry_sigkill_restores_digest_fact_and_queue_together(self):
        for stage in ("after_records", "after_checkpoint", "before_commit", "after_commit"):
            root = self.root / stage
            shared = root / "shared"
            shared.mkdir(parents=True)
            raw = canonical_bytes({"outbox_event": event()})
            (shared / "run.json").write_bytes(raw)
            script = """import os, signal, sys
from pathlib import Path
from sledra_analysis.store.raw import RawStore
from sledra_analysis.ingest.transaction import AtomicIngestor
root, stage = Path(sys.argv[1]), sys.argv[2]
with RawStore(root/'facts.sqlite') as store:
    ingest=AtomicIngestor(store, root/'shared', 'test-root')
    def die(name):
        if name == stage: os.kill(os.getpid(), signal.SIGKILL)
    ingest._stage=die
    ingest.registry('run.json', source_id='registry', collected_at='2026-10-14T00:00:00Z')
"""
            result = subprocess.run(
                [sys.executable, "-c", script, str(root), stage], capture_output=True
            )
            self.assertEqual(result.returncode, -signal.SIGKILL, result.stderr.decode())
            with RawStore(root / "facts.sqlite") as recovered:
                if stage != "after_commit":
                    self.assertEqual(recovered.counts().events, 0)
                    self.assertEqual(recovered.projection_work(), ())
                    self.assertEqual(
                        recovered._db()
                        .execute("SELECT count(*) FROM registry_checkpoints")
                        .fetchone()[0],
                        0,
                    )
                AtomicIngestor(recovered, shared, "test-root").registry(
                    "run.json", source_id="registry", collected_at=COLLECTED
                )
                self.assertEqual(recovered.counts(), StoreCounts(1, 1, 1, 0))
                self.assertEqual(recovered.events()[0].versions[0].sources[0].raw_bytes, raw)
                self.assertEqual(len(recovered.projection_work()), 2)
            CRASH_OBSERVATIONS.append(
                {"stage": "registry_" + stage, "signal": "SIGKILL", "pass": True}
            )

    def test_reprocessing_queue_and_audit_commit_together(self):
        item = record()
        quarantine = self.store.quarantine_stream(
            io.BytesIO(item.raw_bytes),
            location=item.location,
            byte_size=len(item.raw_bytes),
            reason="old_policy",
            validator_version="old-1",
            collected_at=COLLECTED,
        )
        before = self.store.projection_work()
        self.store._db().execute(
            "CREATE TEMP TRIGGER fail_audit BEFORE INSERT ON quarantine_replays BEGIN SELECT RAISE(ABORT, 'fail'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.reprocess(quarantine, EventValidator(), attempted_at=LATER)
        self.assertEqual(self.store.projection_work(), before)
        self.assertEqual(self.store.counts().events, 0)
        self.store._db().execute("DROP TRIGGER fail_audit")
        replay = self.store.reprocess(quarantine, EventValidator(), attempted_at=LATER)
        self.assertEqual(replay.status, "accepted")
        self.assertEqual(
            [w[1] for w in self.store.projection_work()], ["quarantine", "event", "validation"]
        )
        self.assertEqual(self.store.quarantine_bytes(quarantine), item.raw_bytes)

    def test_newer_backup_keeps_source_conflict_after_earlier_valid_ack(self):
        source = self.write()
        self.scan(source)
        receipt = self.ack(source)
        other = self.write(line(2), "other.sealed.jsonl")
        with self.assertRaises(SourceError):
            self.scan(other)
        snapshot = self.recovery.backup(self.store)
        self.recovery.restore(snapshot, self.root / "restored.sqlite")
        with RawStore(self.root / "restored.sqlite") as restored:
            self.assertTrue(all(c.conflict for c in restored.checkpoints("test-root")))
            self.assertEqual(signature(restored), signature(self.store))
        self.recovery.verify_receipt(receipt)
        with self.assertRaises(SourceError):
            self.ack(source)

    def test_existing_ack_cannot_confirm_another_database_lineage(self):
        source = self.write()
        self.scan(source)
        receipt = self.ack(source)
        with RawStore(self.root / "other.sqlite") as other:
            AtomicIngestor(other, self.shared, "test-root").scan(
                source.name, collected_at=COLLECTED
            )
            with self.assertRaises(StoreError):
                self.recovery.acknowledge(
                    other, self.shared, "test-root", source.name, acknowledged_at=LATER
                )
        self.assertEqual(self.ack(source), receipt)


if __name__ == "__main__":
    unittest.main()
