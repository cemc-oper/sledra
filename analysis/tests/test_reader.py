"""V10/V16: streaming boundaries, durable isolation, compatibility and replay."""

from __future__ import annotations

import copy
import hashlib
import io
import json
import sqlite3
import tempfile
import tracemalloc
import unittest
from importlib.resources import files
from pathlib import Path
from unittest.mock import patch

from store_fixture import frozen_records
from test_source import segment
from test_store import COLLECTED, LATER, ROOT, event, fixture, record

from sledra_analysis.ingest.reader import BoundedReader
from sledra_analysis.ingest.source import SourceChanged, SourceTracker
from sledra_analysis.ingest.validation import EventValidator
from sledra_analysis.store.canonical import RecordError, canonical_bytes
from sledra_analysis.store.quarantine import QuarantineError, QuarantineLimit
from sledra_analysis.store.raw import RawStore, StoreCounts, StoreError

MEMORY_OBSERVATIONS = []


def line(index: int = 1, **updates) -> bytes:
    value = event()
    value["event_id"] = f"00000000-0000-4000-8000-{index:012x}"
    value.update(updates)
    return canonical_bytes(value) + b"\n"


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.validator = EventValidator()

    def test_embedded_contract_bytes_match_frozen_authority(self):
        assets = files("sledra_analysis").joinpath("resources/contract")
        for name in ("event.schema.json", "event-types.json"):
            self.assertEqual(
                assets.joinpath(name).read_bytes(), (ROOT / "spec/1.0.0" / name).read_bytes()
            )

    def test_frozen_known_and_future_extension_events_validate(self):
        for name in (
            "valid-bundle.json",
            "valid-future-extension.json",
            "valid-surrogate-escape.json",
        ):
            for value in fixture(name)["events"]:
                result = self.validator.validate(canonical_bytes(value))
                self.assertEqual(result.supported, value["event_type"] in self.validator.catalog)
                self.assertEqual(result.event, value)

    def test_major_enum_required_payload_and_authority_fail_without_raw_detail(self):
        cases = [
            (line(schema_version="2.0.0"), "unsupported_major"),
            (
                line(source={"kind": "future_kind", "source_id": "n", "session_id": "s"}),
                "schema_invalid",
            ),
            (line(payload={}), "schema_invalid"),
            (
                line(producer={"name": "n", "version": "v", "role": "wrong_owner"}),
                "unauthorized_event",
            ),
            (line(event_type="task.success"), "semantic_invalid"),
        ]
        for raw, expected in cases:
            with self.subTest(reason=expected), self.assertRaises(RecordError) as caught:
                self.validator.validate(raw)
            self.assertEqual(caught.exception.code, expected)
            self.assertNotIn(raw.decode(), str(caught.exception))

    def test_exit_fields_and_finish_coverage_are_semantically_checked(self):
        values = fixture("valid-bundle.json")["events"]
        exited = copy.deepcopy(
            next(value for value in values if value["event_type"] == "attempt.exited")
        )
        exited["payload"]["signal"] = 15
        with self.assertRaises(RecordError) as caught:
            self.validator.validate(canonical_bytes(exited))
        self.assertEqual(caught.exception.code, "semantic_invalid")
        finish = copy.deepcopy(
            next(value for value in values if value["event_type"] == "run.finish_declared")
        )
        finish["payload"]["covered_attempts_sha256"] = "a" * 64
        with self.assertRaises(RecordError) as caught:
            self.validator.validate(canonical_bytes(finish))
        self.assertEqual(caught.exception.code, "semantic_invalid")


class ReaderTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="sledra V10 V16 private ")
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.shared = self.root / "shared"
        self.shared.mkdir()
        self.tracker = SourceTracker(self.shared, "test-root")
        self.store = RawStore(self.root / "facts.sqlite")
        self.addCleanup(self.store.close)

    def write(self, body: bytes, name="a.open.jsonl"):
        path = self.shared / name
        path.write_bytes(segment(body))
        return path

    def scan(self, path, *, remember=False, **budgets):
        reader = BoundedReader(self.store, **budgets)
        with self.tracker.open(path.name) as lease:
            result = reader.process(lease, locator=path.name, collected_at=COLLECTED)
        if remember:
            self.tracker.remember(result.checkpoint)
        return result

    def test_one_byte_and_other_chunk_boundaries_preserve_all_unicode_and_bad_bytes(self):
        for chunk in (1, 2, 7, 65536):
            self.tracker = SourceTracker(self.shared, f"root-{chunk}")
            good = line(
                chunk, extensions={"future.text": {"label": "é😀", "line": "escaped\ntext"}}
            )
            raw = good + b"\xff\n" + line(chunk + 100)
            path = self.write(raw, f"{chunk}.open.jsonl")
            result = self.scan(path, chunk_size=chunk)
            self.assertEqual(result.records, 3)
            self.assertEqual(result.checkpoint.committed_offset, path.stat().st_size)
            bad = self.store.quarantine_record(result.quarantine_ids[0])
            self.assertEqual(bad.reason, "invalid_utf8")
            self.assertEqual(self.store.quarantine_bytes(bad.quarantine_id), b"\xff\n")
            self.assertEqual(
                self.store.sources(f"00000000-0000-4000-8000-{chunk:012x}")[0].raw_bytes, good
            )

    def test_bad_json_utf8_duplicate_major_and_missing_payload_do_not_block_later_good(self):
        duplicate = line().replace(b'"event_id":', b'"event_id":"duplicate","event_id":')
        bad_lines = [
            b"{broken\n",
            b"\xff\n",
            duplicate,
            line(schema_version="2.0.0"),
            line(payload={}),
        ]
        path = self.write(b"".join(bad_lines) + line(50))
        result = self.scan(path, remember=True, chunk_size=3)
        self.assertEqual(result.records, 6)
        self.assertEqual(self.store.counts(), StoreCounts(1, 1, 1, 0))
        expected = [
            "invalid_json",
            "invalid_utf8",
            "duplicate_key",
            "unsupported_major",
            "schema_invalid",
        ]
        self.assertEqual([item.reason for item in self.store.quarantines()], expected)
        offset = len(segment(b""))
        for identity, raw in zip(result.quarantine_ids, bad_lines):
            item = self.store.quarantine_record(identity)
            self.assertEqual(
                (item.location.generation, item.location.byte_start, item.location.byte_end),
                (0, offset, offset + len(raw)),
            )
            self.assertEqual(item.raw_hash, hashlib.sha256(raw).hexdigest())
            self.assertEqual(self.store.quarantine_bytes(identity), raw)
            offset += len(raw)
        again = self.scan(path)
        self.assertEqual(again.records, 0)
        self.assertEqual(len(self.store.quarantines()), 5)

    def test_open_tail_never_advances_until_lf_and_then_is_processed_once(self):
        good, partial = line(1), line(2)[:-1]
        path = self.write(good + partial)
        first = self.scan(path, remember=True, chunk_size=1)
        boundary = len(segment(b"")) + len(good)
        self.assertEqual(first.checkpoint.committed_offset, boundary)
        self.assertEqual(first.pending_tail_bytes, len(partial))
        self.assertIn("open_partial_tail", first.diagnostics)
        self.assertEqual(self.store.counts().events, 1)
        self.assertEqual(self.store.quarantines(), ())
        with path.open("ab") as stream:
            stream.write(b"\n")
        second = self.scan(path, remember=True)
        self.assertEqual(second.records, 1)
        self.assertEqual(self.store.counts().events, 2)
        self.assertEqual(second.checkpoint.committed_offset, path.stat().st_size)
        self.assertEqual(self.scan(path).records, 0)

    def test_invalid_open_tail_waits_for_lf_before_quarantine(self):
        path = self.write(b"\xff")
        result = self.scan(path)
        self.assertEqual(result.checkpoint.committed_offset, 0)
        self.assertEqual(self.store.quarantines(), ())
        with path.open("ab") as stream:
            stream.write(b"\n" + line(3))
        result = self.scan(path)
        self.assertEqual(result.records, 2)
        self.assertEqual(self.store.quarantine_bytes(result.quarantine_ids[0]), b"\xff\n")
        self.assertEqual(self.store.counts().events, 1)

    def test_sealed_residual_is_preserved_and_explicit_eof_position_can_resume(self):
        partial = line(2)[:-1]
        path = self.write(line(1) + partial, "a.sealed.jsonl")
        result = self.scan(path, remember=True)
        self.assertEqual(result.checkpoint.committed_offset, path.stat().st_size)
        self.assertEqual(self.store.quarantine_bytes(result.quarantine_ids[0]), partial)
        self.assertEqual(
            self.store.quarantine_record(result.quarantine_ids[0]).reason, "sealed_incomplete_tail"
        )
        self.assertEqual(self.store.counts().events, 1)
        self.assertEqual(self.scan(path).records, 0)

    def test_unknown_minor_critical_type_is_persisted_as_opaque_not_success(self):
        raw = line(
            event_type="future.workflow_completed",
            schema_version="1.9.0",
            payload={"reported_success": True},
            extensions={"future.data": {"x": [1, "01"]}},
        )
        path = self.write(raw)
        result = self.scan(path)
        identity = json.loads(raw)["event_id"]
        self.assertEqual(result.opaque_event_ids, (identity,))
        self.assertIn("unsupported_semantics", result.diagnostics)
        self.assertEqual(self.store.validation_results(identity)[0][2], "opaque")
        self.store.close()
        with RawStore(self.root / "facts.sqlite") as reopened:
            self.assertEqual(reopened.validation_results(identity)[0][2], "opaque")
            self.assertEqual(reopened.sources(identity)[0].raw_bytes, raw)

    def test_oversized_line_streams_to_sqlite_with_bounded_python_memory(self):
        path = self.write(b"")
        size = 8 * 1024 * 1024
        with path.open("ab") as stream:
            for _ in range(size // 65536):
                stream.write(b"X" * 65536)
            stream.write(b"\n" + line(10))
        reader = BoundedReader(self.store, max_line_bytes=1024, chunk_size=4096)
        with self.tracker.open(path.name) as lease:
            tracemalloc.start()
            result = reader.process(lease, locator=path.name, collected_at=COLLECTED)
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        MEMORY_OBSERVATIONS.append(
            {
                "line_bytes": size + 1,
                "peak_python_bytes": peak,
                "line_limit_bytes": 1024,
                "chunk_bytes": 4096,
            }
        )
        self.assertLess(peak, 2 * 1024 * 1024)
        self.assertEqual(result.records, 2)
        isolated = self.store.quarantine_record(result.quarantine_ids[0])
        self.assertEqual((isolated.reason, isolated.byte_size), ("limit_exceeded", size + 1))
        with self.assertRaises(QuarantineLimit):
            self.store.quarantine_bytes(isolated.quarantine_id)
        digest = hashlib.sha256()
        total = 0
        for chunk in self.store.quarantine_chunks(isolated.quarantine_id, chunk_size=4096):
            self.assertLessEqual(len(chunk), 4096)
            digest.update(chunk)
            total += len(chunk)
        self.assertEqual((total, digest.hexdigest()), (size + 1, isolated.raw_hash))
        self.assertEqual(self.store.counts().events, 1)

    def test_long_open_unterminated_tail_is_stuck_without_position_or_blob(self):
        path = self.write(b"X" * 65536)
        result = self.scan(path, max_line_bytes=1024, chunk_size=17)
        self.assertEqual(result.checkpoint.committed_offset, 0)
        self.assertEqual(result.pending_tail_bytes, 65536)
        self.assertIn("stuck_tail", result.diagnostics)
        self.assertEqual(self.store.quarantines(), ())

    def test_record_and_byte_scan_budgets_leave_unread_records_for_next_scan(self):
        path = self.write(line(1) + line(2) + line(3))
        result = self.scan(path, remember=True, max_records=1)
        self.assertIn("scan_record_budget_reached", result.diagnostics)
        self.assertEqual(self.store.counts().events, 1)
        result = self.scan(path, max_scan_bytes=1)
        self.assertIn("scan_byte_budget_reached", result.diagnostics)
        self.assertIn("scan_partial_record", result.diagnostics)
        self.assertEqual(
            result.checkpoint.committed_offset, self.tracker.checkpoints[0].committed_offset
        )
        self.scan(path, remember=True)
        self.assertEqual(self.store.counts().events, 3)

    def test_quarantine_storage_failure_never_returns_or_installs_later_position(self):
        path = self.write(line(1) + b"{bad\n" + line(2))
        self.store._connection.execute(
            "CREATE TRIGGER injected_quarantine_failure BEFORE INSERT ON quarantine "
            "BEGIN SELECT RAISE(ABORT, 'isolation failure'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.scan(path, remember=True)
        self.assertEqual(self.tracker.checkpoints[0].committed_offset, 0)
        self.assertEqual(self.store.counts().events, 1)
        self.assertEqual(self.store.quarantines(), ())
        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM quarantine_blobs").fetchone()[0], 0
        )
        self.store._connection.execute("DROP TRIGGER injected_quarantine_failure")
        self.scan(path, remember=True)
        self.assertEqual(self.store.counts().events, 2)
        self.assertEqual(len(self.store.quarantines()), 1)

    def test_quarantine_commit_failure_from_reader_lock_does_not_advance(self):
        path = self.write(b"{bad\n")
        external = sqlite3.connect(self.store.path, isolation_level=None)
        self.addCleanup(external.close)
        external.execute("BEGIN")
        external.execute("SELECT * FROM quarantine").fetchall()
        with self.assertRaises(sqlite3.OperationalError):
            self.scan(path, remember=True)
        self.assertEqual(self.tracker.checkpoints[0].committed_offset, 0)
        self.assertEqual(self.store.quarantines(), ())
        external.execute("ROLLBACK")
        self.assertEqual(len(self.scan(path).quarantine_ids), 1)

    def test_per_blob_and_total_capacity_failures_block_position(self):
        self.store.close()
        self.store = RawStore(
            self.root / "limited.sqlite", max_quarantine_blob_bytes=4, max_quarantine_total_bytes=4
        )
        self.addCleanup(self.store.close)
        path = self.write(b"12345\n")
        with self.assertRaises(QuarantineLimit):
            self.scan(path)
        self.assertEqual(self.tracker.checkpoints[0].committed_offset, 0)
        self.tracker = SourceTracker(self.shared, "quota-root")
        path = self.write(b"\xff\n\xfe\n\xfd\n")
        with self.assertRaises(QuarantineLimit):
            self.scan(path)
        self.assertEqual(self.tracker.checkpoints[0].committed_offset, 0)
        self.assertEqual(len(self.store.quarantines()), 2)

    def test_duplicate_quarantine_retries_and_identical_blob_origins_are_idempotent(self):
        path = self.write(b"\xff\n\xff\n")
        first = self.scan(path)
        second = self.scan(path)
        self.assertEqual(first.quarantine_ids, second.quarantine_ids)
        self.assertEqual(len(self.store.quarantines()), 2)
        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM quarantine_blobs").fetchone()[0], 1
        )
        page = self.store.quarantines(limit=1)
        self.assertEqual(len(page), 1)
        self.assertEqual(len(self.store.quarantines(after_id=page[0].quarantine_id)), 1)

    def test_source_change_during_persistence_raises_without_checkpoint(self):
        path = self.write(line(1))
        real = self.store.record_validated

        def changed(*args, **kwargs):
            real(*args, **kwargs)
            with path.open("ab") as stream:
                stream.write(line(2))

        with patch.object(self.store, "record_validated", side_effect=changed):
            with self.assertRaises(SourceChanged):
                self.scan(path, remember=True)
        self.assertEqual(self.tracker.checkpoints[0].committed_offset, 0)
        self.scan(path)
        self.assertEqual(self.store.counts().events, 2)

    def test_quarantine_generation_changes_preserve_original_origins(self):
        path = self.write(b"\xff\n" + line(1))
        self.scan(path, remember=True)
        path.write_bytes(segment(b"\xfe\n"))
        self.scan(path)
        self.assertEqual([item.location.generation for item in self.store.quarantines()], [0, 1])
        self.assertEqual(
            [self.store.quarantine_bytes(item.quarantine_id) for item in self.store.quarantines()],
            [b"\xff\n", b"\xfe\n"],
        )

    def test_reprocessing_retains_unknown_major_and_sealed_tail_without_repair(self):
        path = self.write(line(schema_version="2.0.0") + line(2)[:-1], "a.sealed.jsonl")
        result = self.scan(path)
        before = self.store.quarantines()
        for identity in result.quarantine_ids:
            replay = self.store.reprocess(identity, EventValidator(), attempted_at=LATER)
            self.assertEqual(replay.status, "retained")
        self.assertEqual(self.store.quarantines(), before)
        self.assertEqual(self.store.counts().events, 0)

    def test_new_validator_reprocesses_exact_original_bytes_with_audited_acceptance(self):
        class Older(EventValidator):
            version = "site-limited-0.1"

            def validate(self, raw, *, event_pointer=""):
                raise RecordError("not supported by this policy", "unsupported_semantics")

        raw = line(1)
        path = self.write(raw)
        result = self.scan(path, validator=Older())
        identity = result.quarantine_ids[0]
        before = self.store.quarantine_record(identity)
        accepted = self.store.reprocess(identity, EventValidator(), attempted_at=LATER)
        self.assertEqual(accepted.status, "accepted")
        self.assertEqual(self.store.sources(json.loads(raw)["event_id"])[0].raw_bytes, raw)
        self.assertEqual(self.store.quarantine_record(identity), before)
        self.assertEqual(self.store.quarantine_bytes(identity), raw)
        self.store.reprocess(identity, EventValidator(), attempted_at=LATER)
        self.assertEqual(len(self.store.replays(identity)), 2)
        self.assertEqual(self.store.counts(), StoreCounts(1, 1, 1, 0))

    def test_reprocessing_audit_failure_rolls_back_fact_and_validation_but_retains_input(self):
        raw = line(1)
        item = record(raw=raw)
        with io.BytesIO(raw) as stream:
            identity = self.store.quarantine_stream(
                stream,
                location=item.location,
                byte_size=len(raw),
                reason="schema_invalid",
                validator_version="older-0.1",
                collected_at=COLLECTED,
            )
        self.store._connection.execute(
            "CREATE TRIGGER failed_replay BEFORE INSERT ON quarantine_replays "
            "BEGIN SELECT RAISE(ABORT, 'replay audit failure'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.reprocess(identity, EventValidator(), attempted_at=LATER)
        self.assertEqual(self.store.counts().events, 0)
        self.assertEqual(self.store.replays(identity), ())
        self.assertEqual(self.store.quarantine_bytes(identity), raw)
        self.assertEqual(self.store.validation_results(json.loads(raw)["event_id"]), ())
        self.store._connection.execute("DROP TRIGGER failed_replay")
        self.assertEqual(
            self.store.reprocess(identity, EventValidator(), attempted_at=LATER).status, "accepted"
        )

    def test_canonical_byte_expansion_is_isolated_without_partial_fact(self):
        value = json.loads(line(1))
        value["extensions"] = {"future.text": {"text": "\u2028" * 500}}
        raw = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        self.store.close()
        self.store = RawStore(self.root / "canonical-limit.sqlite", max_record_bytes=len(raw))
        self.addCleanup(self.store.close)
        result = self.scan(self.write(raw))
        self.assertEqual(self.store.counts().events, 0)
        self.assertEqual(
            self.store.quarantine_record(result.quarantine_ids[0]).reason, "limit_exceeded"
        )
        self.assertEqual(self.store.quarantine_bytes(result.quarantine_ids[0]), raw)

    def test_reprocess_opaque_and_oversize_do_not_claim_supported_semantics(self):
        unknown = line(event_type="future.completed", payload={"success": True})
        with io.BytesIO(unknown) as stream:
            item = record(raw=unknown)
            identity = self.store.quarantine_stream(
                stream,
                location=item.location,
                byte_size=len(unknown),
                reason="unsupported_semantics",
                validator_version="older-0.1",
                collected_at=COLLECTED,
            )
        replay = self.store.reprocess(identity, EventValidator(), attempted_at=LATER)
        self.assertEqual(replay.status, "opaque")
        self.assertEqual(self.store.validation_results(replay.event_id)[0][2], "opaque")
        path = self.write(b"X" * (1024 * 1024 + 1) + b"\n")
        identity = self.scan(path).quarantine_ids[0]
        with patch.object(
            self.store, "quarantine_bytes", side_effect=AssertionError("must not materialize")
        ):
            replay = self.store.reprocess(identity, EventValidator(), attempted_at=LATER)
        self.assertEqual((replay.status, replay.reason), ("retained", "limit_exceeded"))

    def test_quarantine_stream_change_and_short_input_roll_back_all_blob_data(self):
        class Changing(io.BytesIO):
            def __init__(self):
                super().__init__(b"bad\n")
                self.seeks = 0

            def seek(self, *args):
                self.seeks += 1
                if self.seeks == 2:
                    self.getbuffer()[:] = b"BAD\n"
                return super().seek(*args)

        location = record(raw=b"bad\n").location
        for stream in (Changing(), io.BytesIO(b"ba")):
            with self.subTest(stream=type(stream).__name__), self.assertRaises(QuarantineError):
                self.store.quarantine_stream(
                    stream,
                    location=location,
                    byte_size=4,
                    reason="invalid_json",
                    validator_version="test-1",
                    collected_at=COLLECTED,
                )
        self.assertEqual(self.store.quarantines(), ())
        self.assertEqual(
            self.store._connection.execute("SELECT count(*) FROM quarantine_blobs").fetchone()[0], 0
        )

    def test_schema_one_explicit_upgrade_preserves_all_raw_facts_and_rejects_implicit_upgrade(self):
        self.store.ingest([record()])
        expected = self.store.events()
        self.store.close()
        with sqlite3.connect(self.root / "facts.sqlite") as connection:
            for trigger in ("enqueue_event", "enqueue_validation", "enqueue_quarantine"):
                connection.execute(f"DROP TRIGGER {trigger}")
            for table in (
                "raw_archives",
                "registry_checkpoints",
                "checkpoints",
                "projection_work_queue",
                "ingestion_commits",
                "ingestion_identity",
                "quarantine_replays",
                "event_validations",
                "quarantine",
                "quarantine_blobs",
            ):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("PRAGMA user_version = 1")
        with self.assertRaises(ValueError):
            RawStore(self.root / "facts.sqlite", upgrade="yes")
        before = (self.root / "facts.sqlite").read_bytes()
        with self.assertRaises(StoreError):
            RawStore(self.root / "facts.sqlite")
        self.assertEqual((self.root / "facts.sqlite").read_bytes(), before)
        with RawStore(self.root / "facts.sqlite", upgrade=True) as migrated:
            self.assertEqual(migrated.events(), expected)
            self.assertEqual(migrated.quarantines(), ())
            self.assertEqual(migrated._connection.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(
                migrated._connection.execute("PRAGMA foreign_key_check").fetchall(), []
            )

    def test_schema_upgrade_commit_failure_leaves_version_one_and_no_partial_tables(self):
        self.store.ingest([record()])
        self.store.close()
        path = self.root / "facts.sqlite"
        with sqlite3.connect(path) as connection:
            for trigger in ("enqueue_event", "enqueue_validation", "enqueue_quarantine"):
                connection.execute(f"DROP TRIGGER {trigger}")
            for table in (
                "raw_archives",
                "registry_checkpoints",
                "checkpoints",
                "projection_work_queue",
                "ingestion_commits",
                "ingestion_identity",
                "quarantine_replays",
                "event_validations",
                "quarantine",
                "quarantine_blobs",
            ):
                connection.execute(f"DROP TABLE {table}")
            connection.execute("PRAGMA user_version = 1")
        reader = sqlite3.connect(path, isolation_level=None)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM events").fetchall()
        with self.assertRaises(sqlite3.OperationalError):
            RawStore(path, upgrade=True)
        self.assertEqual(reader.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertEqual(
            reader.execute(
                "SELECT count(*) FROM sqlite_master WHERE name = 'quarantine'"
            ).fetchone()[0],
            0,
        )
        reader.execute("ROLLBACK")
        with RawStore(path, upgrade=True) as migrated:
            self.assertEqual(migrated.counts(), StoreCounts(1, 1, 1, 0))

    def test_frozen_19_facts_remain_valid_and_segment_reader_does_not_add_spurious_quarantine(self):
        inputs, _ = frozen_records(self.root / "frozen")
        self.store.ingest(inputs)
        tracker = SourceTracker(self.root / "frozen", "frozen-root")
        reader = BoundedReader(self.store, chunk_size=1)
        segments = sorted(
            {item.location.locator for item in inputs if item.location.source_kind == "segment"}
        )
        total = 0
        for locator in segments:
            with tracker.open(locator) as lease:
                result = reader.process(lease, locator=locator, collected_at=COLLECTED)
                total += result.records
                self.assertEqual(result.quarantine_ids, ())
                self.assertEqual(result.opaque_event_ids, ())
        self.assertEqual(total, 11)
        self.assertEqual(self.store.counts(), StoreCounts(19, 19, 19, 0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
