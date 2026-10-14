"""V09: real SQLite replay, canonical golden bytes, immutable variants and sources."""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from dataclasses import replace
from pathlib import Path

from store_fixture import frozen_records

from sledra_analysis.ingest.source import SourceKey
from sledra_analysis.store.canonical import RecordError, canonical_bytes, canonical_hash, parse_json
from sledra_analysis.store.raw import (
    APPLICATION_ID,
    RawRecord,
    RawStore,
    SourceLocation,
    StoreCounts,
    StoreError,
    WriterBusy,
)

ROOT = Path(__file__).resolve().parents[2]
COLLECTED = "2026-10-14T00:00:00Z"
LATER = "2026-10-14T01:00:00Z"


def fixture(name):
    return json.loads((ROOT / "spec/fixtures/1.0.0" / name).read_bytes())


def event():
    return copy.deepcopy(fixture("valid-bundle.json")["events"][0])


def record(value=None, *, locator="first.jsonl", raw=None, generation=0, start=0):
    if raw is None:
        raw = canonical_bytes(event() if value is None else value) + b"\n"
    location = SourceLocation(
        SourceKey("test-root", "test-node", "test-segment"),
        generation,
        start,
        start + len(raw),
        locator,
    )
    return RawRecord(raw, location, COLLECTED)


class CanonicalTests(unittest.TestCase):
    def test_frozen_golden_values_and_complete_event_hash(self):
        golden = fixture("golden-vectors.json")
        self.assertEqual(
            canonical_hash(fixture("valid-bundle.json")["events"][5]), golden["event_sha256"]
        )
        for field, value in (
            ("canonical_parameters", {"domain": "global", "lead_seconds": 0, "member": "01"}),
            ("key_order", {"z": 1, "a": 2}),
            ("negative_zero_canonical", parse_json(b'{"n":-0}')),
            ("string_01", {"member": "01"}),
            ("integer_1", {"member": 1}),
            ("unicode_nfc", {"emoji": "☀", "label": "e\u0301"}),
        ):
            self.assertEqual(canonical_bytes(value), golden[field].encode())

    def test_unicode_control_escape_surrogate_pairs_and_integer_extremes(self):
        self.assertEqual(
            canonical_bytes(parse_json(b'{"x":"\\ud83d\\ude00"}')), '{"x":"😀"}'.encode()
        )
        self.assertEqual(
            canonical_bytes({"x": "\n\x00\u2028\u2029"}), b'{"x":"\\n\\u0000\\u2028\\u2029"}'
        )
        self.assertEqual(
            canonical_bytes([-(1 << 63), (1 << 63) - 1]),
            b"[-9223372036854775808,9223372036854775807]",
        )
        self.assertNotEqual(canonical_hash({"x": True}), canonical_hash({"x": 1}))

    def test_reject_duplicate_normalized_keys_numbers_surrogates_and_bad_utf8(self):
        cases = [
            b'{"a":1,"a":2}',
            '{"é":1,"e\\u0301":2}'.encode(),
            b'{"n":1.0}',
            b'{"n":1e0}',
            b'{"n":NaN}',
            b'{"n":Infinity}',
            b'{"n":9223372036854775808}',
            b'{"n":-9223372036854775809}',
            b'{"x":"\\ud800"}',
            b'{"x":"\\udc00"}',
            b"\xff",
            "{}".encode("utf-16"),
            b"{} trailing",
            b"[" * 1000 + b"]" * 1000,
        ]
        for raw in cases:
            with self.subTest(raw=raw[:60]), self.assertRaises(RecordError):
                parse_json(raw)
        with self.assertRaises(RecordError):
            canonical_bytes({1: "wrong-key"})
        with self.assertRaises(RecordError):
            canonical_bytes({"n": 1.0})


class RawStoreTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="sledra V09 SQLite with spaces ")
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.path = self.root / "facts.sqlite"
        self.store = RawStore(self.path)
        self.addCleanup(self.store.close)

    def test_replay_same_source_is_idempotent_and_persists_raw_bytes(self):
        original = record(raw=json.dumps(event(), ensure_ascii=True, indent=2).encode() + b"\n")
        first = self.store.ingest([original])
        second = self.store.ingest([replace(original, collected_at=LATER)] * 3)
        self.assertEqual((first.new_events, first.new_versions, first.new_sources), (1, 1, 1))
        self.assertEqual((second.new_events, second.new_versions, second.new_sources), (0, 0, 0))
        self.assertEqual(self.store.counts(), StoreCounts(1, 1, 1, 0))
        source = self.store.sources(event()["event_id"])[0]
        self.assertEqual(source.raw_bytes, original.raw_bytes)
        self.assertEqual(source.raw_hash, hashlib.sha256(original.raw_bytes).hexdigest())
        self.assertEqual(source.collected_at, COLLECTED)
        self.store.close()
        with RawStore(self.path) as reopened:
            self.assertEqual(reopened.sources(event()["event_id"])[0], source)
            self.assertEqual(reopened.counts(), StoreCounts(1, 1, 1, 0))

    def test_equivalent_encoding_adds_raw_sources_without_new_event_version(self):
        value = event()
        value["extensions"] = {"future.text": {"label": "é", "emoji": "😀", "count": 0}}
        alternate = copy.deepcopy(value)
        alternate["extensions"]["future.text"]["label"] = "e\u0301"
        a = record(value)
        b = record(
            raw=json.dumps(alternate, ensure_ascii=True, indent=2).encode(), locator="copy.jsonl"
        )
        self.store.ingest([a, b])
        self.assertEqual(self.store.counts(), StoreCounts(1, 1, 2, 0))
        stored = self.store.get_event(value["event_id"])
        self.assertFalse(stored.conflicted)
        self.assertEqual(stored.versions[0].canonical_bytes, canonical_bytes(value))
        self.assertEqual(
            {source.raw_bytes for source in stored.versions[0].sources}, {a.raw_bytes, b.raw_bytes}
        )

    def test_alias_namespace_generation_and_offset_are_distinct_provenance(self):
        a = record()
        changes = [
            replace(a.location, locator="copy.jsonl"),
            replace(a.location, generation=1),
            replace(a.location, byte_start=10, byte_end=10 + len(a.raw_bytes)),
            replace(a.location, key=replace(a.location.key, source_root_id="other-root")),
            replace(a.location, key=replace(a.location.key, source_id="other-node")),
            replace(a.location, key=replace(a.location.key, segment_uuid="other-segment")),
        ]
        self.store.ingest([a, *(replace(a, location=location) for location in changes)])
        self.assertEqual(self.store.counts(), StoreCounts(1, 1, 7, 0))
        self.assertEqual(
            {source.location for source in self.store.sources(event()["event_id"])},
            {a.location, *changes},
        )

    def test_payload_recorded_at_and_unknown_extensions_all_create_conflicts(self):
        original = event()
        versions = [original]
        payload = dict(original["payload"], request_sha256="a" * 64)
        for field, value in (
            ("payload", payload),
            ("recorded_at", "2026-10-14T00:00:00Z"),
            ("extensions", {"future.data": {"value": 9}}),
        ):
            modified = copy.deepcopy(original)
            modified[field] = value
            versions.append(modified)
        inputs = [
            record(value, locator=f"version-{index}.jsonl") for index, value in enumerate(versions)
        ]
        result = self.store.ingest(inputs)
        self.assertEqual(result.conflicting_event_ids, (original["event_id"],))
        self.assertEqual(self.store.counts(), StoreCounts(1, 4, 4, 1))
        stored = self.store.get_event(original["event_id"])
        self.assertTrue(stored.conflicted)
        self.assertEqual(
            {version.canonical_bytes for version in stored.versions},
            {canonical_bytes(value) for value in versions},
        )
        self.assertEqual(
            {source.raw_bytes for version in stored.versions for source in version.sources},
            {item.raw_bytes for item in inputs},
        )
        self.assertEqual(self.store.events(conflicts_only=True), (stored,))
        self.store.ingest(inputs)
        self.assertEqual(self.store.counts(), StoreCounts(1, 4, 4, 1))

    def test_frozen_same_id_counterexample_preserves_both_versions(self):
        events = fixture("invalid-conflicting-event-id.json")["events"]
        pairs = [
            pair
            for pair in itertools.combinations(events, 2)
            if pair[0]["event_id"] == pair[1]["event_id"]
        ]
        self.assertEqual(len(pairs), 1)
        a, b = pairs[0]
        self.store.ingest([record(a), record(b, locator="conflict.jsonl")])
        result = self.store.get_event(a["event_id"])
        self.assertTrue(result.conflicted)
        self.assertEqual(
            {version.canonical_hash for version in result.versions},
            {canonical_hash(a), canonical_hash(b)},
        )

    def test_input_permutations_and_retransmission_preserve_identical_fact_sets(self):
        a, b = event(), event()
        b["recorded_at"] = "2026-10-14T02:00:00Z"
        inputs = [record(a), record(b, locator="second.jsonl"), record(a, locator="copy.jsonl")]
        expected = None
        for index, order in enumerate(itertools.permutations(inputs)):
            with RawStore(self.root / f"order-{index}.sqlite") as store:
                store.ingest([*order, *reversed(order), *order])
                state = store.events()
                if expected is None:
                    expected = state
                self.assertEqual(state, expected)
                self.assertEqual(store.counts(), StoreCounts(1, 2, 3, 1))

    def test_unknown_minor_event_and_all_unknown_data_remain_opaque(self):
        value = event()
        value.update(
            schema_version="1.7.3",
            event_type="future.completed",
            extensions={"vendor.future": {"unknown": [1, "01", {"nested": True}]}},
        )
        self.store.ingest([record(value)])
        stored = self.store.get_event(value["event_id"])
        self.assertEqual(parse_json(stored.versions[0].canonical_bytes), value)
        self.assertEqual(stored.versions[0].event_type, "future.completed")
        self.assertFalse(stored.conflicted)

    def test_registry_outbox_retains_the_entire_original_wrapper(self):
        value = event()
        raw = json.dumps(
            {"immutable_metadata": "original wrapper", "outbox_event": value},
            ensure_ascii=False,
            indent=2,
        ).encode()
        item = record(raw=raw)
        item = replace(
            item,
            location=replace(item.location, source_kind="registry"),
            event_pointer="/outbox_event",
        )
        self.store.ingest([item, item])
        stored = self.store.get_event(value["event_id"])
        self.assertEqual(stored.versions[0].canonical_hash, canonical_hash(value))
        self.assertEqual(stored.versions[0].sources[0].raw_bytes, raw)
        self.assertEqual(stored.versions[0].sources[0].event_pointer, "/outbox_event")
        self.assertEqual(self.store.counts(), StoreCounts(1, 1, 1, 0))

    def test_same_location_with_different_bytes_keeps_all_raw_records(self):
        value = event()
        a = record(value)
        # Same ID/location/length, changed payload byte; storage must never overwrite.
        b = copy.deepcopy(value)
        b["producer"]["name"] = value["producer"]["name"].replace("synthetic", "SYNTHETIC")
        changed = record(b)
        self.assertEqual(a.location, changed.location)
        self.store.ingest([a, changed])
        self.assertEqual(self.store.counts(), StoreCounts(1, 2, 2, 1))

    def test_bad_record_rolls_back_the_whole_batch_before_writing(self):
        for raw in (b'{"event_id":', b"\xff", b'{"a":1,"a":2}', b"{}"):
            with self.subTest(raw=raw), self.assertRaises(RecordError):
                self.store.ingest([record(), record(raw=raw, locator="bad.jsonl")])
            self.assertEqual(self.store.counts(), StoreCounts(0, 0, 0, 0))
        for updates in (
            {"schema_version": "2.0.0"},
            {"event_id": "invalid"},
            {"recorded_at": "no-timezone"},
            {"payload": []},
        ):
            invalid = event()
            invalid.update(updates)
            with self.subTest(updates=updates), self.assertRaises(RecordError):
                self.store.ingest([record(), record(invalid)])
        self.assertEqual(self.store.counts(), StoreCounts(0, 0, 0, 0))

    def test_position_metadata_and_pointer_validation(self):
        valid = record()
        invalid = [
            replace(valid, collected_at="2026-10-14"),
            replace(valid, location=replace(valid.location, byte_end=1)),
            replace(valid, location=replace(valid.location, generation=-1)),
            replace(valid, location=replace(valid.location, generation=True)),
            replace(valid, location=replace(valid.location, byte_start=1 << 63)),
            replace(valid, location=replace(valid.location, source_kind="other")),
            replace(valid, location=replace(valid.location, locator="")),
            replace(valid, event_pointer="/outbox_event"),
            replace(valid, location=replace(valid.location, source_kind="registry")),
        ]
        for item in invalid:
            with self.subTest(item=item.location), self.assertRaises(RecordError):
                self.store.ingest([item])
        self.assertEqual(self.store.counts(), StoreCounts(0, 0, 0, 0))

    def test_record_batch_and_canonical_byte_limits(self):
        item = record()
        for index, budget in enumerate(
            (
                {"max_record_bytes": len(item.raw_bytes) - 1},
                {"max_batch_records": 1},
                {"max_batch_bytes": len(item.raw_bytes) * 2 - 1},
            )
        ):
            with RawStore(self.root / f"budget-{index}.sqlite", **budget) as store:
                with self.assertRaises(RecordError):
                    store.ingest([item, item])
                self.assertEqual(store.counts(), StoreCounts(0, 0, 0, 0))
        expanded = event()
        expanded["extensions"] = {"future.text": {"text": "\u2028" * 500}}
        raw = json.dumps(expanded, ensure_ascii=False, separators=(",", ":")).encode()
        with RawStore(self.root / "canonical-budget.sqlite", max_record_bytes=len(raw)) as store:
            with self.assertRaises(RecordError):
                store.ingest([record(raw=raw)])

    def test_database_failure_rolls_back_events_raw_and_conflicts(self):
        a = event()
        b = copy.deepcopy(a)
        b["recorded_at"] = LATER
        self.store.ingest([record(a)])
        before = self.store.events()
        # Fail late, after event_versions and the conflict trigger have run.
        self.store._connection.execute(
            "CREATE TRIGGER injected_failure BEFORE INSERT ON raw_records "
            "BEGIN SELECT RAISE(ABORT, 'injected storage failure'); END"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.ingest([record(b, locator="failed.jsonl")])
        self.assertEqual(self.store.events(), before)
        self.assertEqual(self.store.counts(), StoreCounts(1, 1, 1, 0))
        self.store._connection.execute("DROP TRIGGER injected_failure")
        self.store.ingest([record(b, locator="failed.jsonl")])
        self.assertEqual(self.store.counts(), StoreCounts(1, 2, 2, 1))

    def test_commit_failure_from_reader_lock_rolls_back_then_retry_succeeds(self):
        reader = sqlite3.connect(self.path, isolation_level=None)
        self.addCleanup(reader.close)
        reader.execute("BEGIN")
        reader.execute("SELECT * FROM events").fetchall()
        with self.assertRaises(sqlite3.OperationalError):
            self.store.ingest([record()])
        self.assertEqual(self.store.counts(), StoreCounts(0, 0, 0, 0))
        reader.execute("ROLLBACK")
        self.store.ingest([record()])
        self.assertEqual(self.store.counts(), StoreCounts(1, 1, 1, 0))

    def test_hash_collision_never_overwrites_existing_bytes(self):
        original = record()
        prepared = self.store._prepare(original)
        self.store.ingest([original])
        before = self.store.events()
        changed = event()
        changed["recorded_at"] = LATER
        item = record(changed, locator="conflicting.jsonl")
        replacement = self.store._prepare(item)
        with patch.object(
            self.store,
            "_prepare",
            return_value=(replacement[0], replacement[1], prepared[2], replacement[3]),
        ):
            with self.assertRaises(StoreError):
                self.store.ingest([item])
        encoded = record(raw=json.dumps(event(), indent=2).encode(), locator="encoded.jsonl")
        replacement = self.store._prepare(encoded)
        with patch.object(
            self.store,
            "_prepare",
            return_value=(replacement[0], replacement[1], replacement[2], prepared[3]),
        ):
            with self.assertRaises(StoreError):
                self.store.ingest([encoded])
        self.assertEqual(self.store.events(), before)
        self.assertEqual(self.store.counts(), StoreCounts(1, 1, 1, 0))

    def test_schema_uniqueness_foreign_keys_types_and_ranges(self):
        item = record()
        self.store.ingest([item])
        connection = self.store._connection
        bad_statements = [
            ("INSERT INTO events VALUES(?)", (event()["event_id"],)),
            (
                "INSERT INTO event_versions VALUES(?, ?, ?, ?, ?)",
                ("missing", "a" * 64, b"{}", "future.unknown", "1.0.0"),
            ),
            ("INSERT INTO raw_blobs VALUES(?, ?)", ("a" * 64, "not bytes")),
            (
                "INSERT INTO source_locations VALUES(NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("root", "node", "segment", -1, 0, 1, "file", "segment"),
            ),
            (
                "INSERT INTO source_locations VALUES(NULL, ?, ?, ?, ?, ?, ?, ?, ?)",
                ("root", "node", "segment", 0, 2, 1, "file", "segment"),
            ),
            (
                "INSERT INTO raw_records VALUES(NULL, ?, ?, ?, ?, ?, ?)",
                (999, "a" * 64, event()["event_id"], "a" * 64, "", COLLECTED),
            ),
        ]
        for sql, parameters in bad_statements:
            with self.subTest(sql=sql), self.assertRaises(sqlite3.IntegrityError):
                connection.execute(sql, parameters)
        self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")

    def test_single_writer_rejects_aliases_and_another_process_then_releases(self):
        with self.assertRaises(WriterBusy):
            RawStore(self.path)
        alias = self.root / "alias.sqlite"
        os.link(self.path, alias)
        with self.assertRaises(WriterBusy):
            RawStore(alias)
        child = subprocess.run(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; import sys; "
                "from sledra_analysis.store.raw import RawStore, WriterBusy; "
                "\ntry: RawStore(Path(sys.argv[1]))\nexcept WriterBusy: sys.exit(37)",
                str(self.path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(child.returncode, 37, child.stderr)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.store.close()
        with RawStore(self.path) as next_writer:
            next_writer.ingest([record()])
            self.assertEqual(next_writer.counts().events, 1)

    def test_unknown_schema_and_canonical_version_are_not_silently_replaced(self):
        unknown = self.root / "foreign.sqlite"
        with sqlite3.connect(unknown) as connection:
            connection.execute("CREATE TABLE foreign_data(value TEXT)")
            connection.execute("INSERT INTO foreign_data VALUES('preserve')")
        with self.assertRaises(StoreError):
            RawStore(unknown)
        with sqlite3.connect(unknown) as connection:
            self.assertEqual(
                connection.execute("SELECT * FROM foreign_data").fetchall(), [("preserve",)]
            )
        self.store.close()
        with sqlite3.connect(self.path) as connection:
            connection.execute("UPDATE store_metadata SET canonical_version = 'future-version'")
            self.assertEqual(
                connection.execute("PRAGMA application_id").fetchone()[0], APPLICATION_ID
            )
        with self.assertRaises(StoreError):
            RawStore(self.path)

    def test_nested_transactions_and_closed_queries_are_rejected(self):
        with self.store.transaction():
            with self.assertRaises(StoreError):
                self.store.ingest([record()])
        self.store.close()
        with self.assertRaises(StoreError):
            self.store.counts()
        with RawStore(self.root / "empty.sqlite") as empty:
            self.assertIsNone(empty.get_event("unknown"))

    def test_database_symlink_and_fifo_are_rejected_without_opening_a_store(self):
        alias = self.root / "symlink.sqlite"
        alias.symlink_to(self.path)
        fifo = self.root / "fifo.sqlite"
        os.mkfifo(fifo)
        for path in (alias, fifo):
            with self.subTest(path=path), self.assertRaises((OSError, StoreError)):
                RawStore(path)

    def test_frozen_handoff_real_sqlite_replay_is_19_facts_not_projections(self):
        inputs, original = frozen_records(self.root / "shared")
        self.assertEqual(len(inputs), 19)
        random.Random(11).shuffle(inputs)
        first = self.store.ingest(inputs)
        self.assertEqual(first.new_events, 19)
        self.assertEqual(self.store.counts(), StoreCounts(19, 19, 19, 0))
        for _ in range(3):
            random.Random(11).shuffle(inputs)
            result = self.store.ingest(inputs)
            self.assertEqual(
                (result.new_events, result.new_versions, result.new_sources), (0, 0, 0)
            )
        for stored in self.store.events():
            for version in stored.versions:
                for source in version.sources:
                    location = source.location
                    self.assertEqual(
                        source.raw_bytes,
                        original[location.locator][location.byte_start : location.byte_end],
                    )
        self.store.close()
        with RawStore(self.path) as reopened:
            self.assertEqual(reopened.counts(), StoreCounts(19, 19, 19, 0))
            self.assertEqual(len(reopened.events()), 19)


if __name__ == "__main__":
    unittest.main(verbosity=2)
