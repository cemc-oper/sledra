"""V11: real file mutations, conflict retention and bounded-integrity counterexamples."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path
from unittest.mock import patch

from sledra_analysis.ingest.source import (
    WINDOW,
    Checkpoint,
    SourceChanged,
    SourceError,
    SourceKey,
    SourceTracker,
)

UUID_A = "8c95fb7d-cf52-49ac-9815-444f1f3744bc"
UUID_B = "8c95fb7d-cf52-49ac-9815-444f1f3744bd"
ROOT = Path(__file__).resolve().parents[2]


def segment(payload: bytes = b'{"event":1}\n', uuid: str = UUID_A) -> bytes:
    header = {
        "record_kind": "segment_header",
        "protocol_version": "1.0.0",
        "source_id": "test-node",
        "segment_uuid": uuid,
        "writer_session": "test-session",
        "created_at": "2026-10-14T00:00:00Z",
    }
    return json.dumps(header, separators=(",", ":")).encode() + b"\n" + payload


class SourceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="sledra V11 with spaces ")
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.tracker = SourceTracker(self.root, "root-1")

    def write(self, name, raw):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        return path

    def observe(self, name, *, commit=False, legacy=None):
        with self.tracker.open(name, legacy_source_id=legacy) as lease:
            if commit:
                self.tracker.remember(lease.checkpoint(lease.decision.checkpoint.last_size))
            return lease.decision

    def test_append_and_checkpoint_restore(self):
        original = segment()
        path = self.write("first.open.jsonl", original)
        first = self.observe(path.name, commit=True)
        stored = asdict(self.tracker.checkpoints[0])
        stored["key"] = SourceKey(**stored["key"])
        stored["discovered_paths"] = tuple(stored["discovered_paths"])
        self.tracker = SourceTracker(self.root, "root-1", (Checkpoint(**stored),))
        with path.open("ab") as stream:
            stream.write(b'{"event":2}\n')
        with self.tracker.open(path.name) as lease:
            self.assertEqual(lease.decision.resume_offset, len(original))
            self.assertEqual(lease.decision.checkpoint.generation, first.checkpoint.generation)
            self.assertEqual(lease.read(len(original), 100), b'{"event":2}\n')
            self.assertEqual(lease.decision.diagnostics, ())

    def test_rename_and_hard_link_rotation_share_generation(self):
        path = self.write("first.open.jsonl", segment())
        self.observe(path.name, commit=True)
        sealed = self.root / "first.sealed.jsonl"
        os.link(path, sealed)
        opened = self.observe(path.name)
        rotated = self.observe(sealed.name)
        self.assertEqual(rotated.checkpoint.generation, opened.checkpoint.generation)
        self.assertEqual(rotated.resume_offset, len(segment()))
        self.assertEqual(rotated.checkpoint.sealed_hash, hashlib.sha256(segment()).hexdigest())
        path.unlink()
        again = self.observe(sealed.name)
        self.assertEqual(again.checkpoint.generation, 0)
        self.assertEqual(again.checkpoint.discovered_paths, (path.name, sealed.name))

    def test_lease_keeps_old_descriptor_during_rename_or_replacement(self):
        path = self.write("first.open.jsonl", segment())
        with self.tracker.open(path.name) as lease:
            path.rename(self.root / "first.sealed.jsonl")
            self.write(path.name, segment(uuid=UUID_B))
            self.assertEqual(lease.read(0, len(segment())), segment())
        replaced = self.observe(path.name)
        self.assertEqual(replaced.resume_offset, 0)
        self.assertEqual(replaced.checkpoint.key.segment_uuid, UUID_B)
        self.assertIn("source_gap", replaced.diagnostics)

    def test_new_uuid_and_same_path_replacement(self):
        path = self.write("same.open.jsonl", segment())
        first = self.observe(path.name, commit=True)
        replacement = self.write("replacement", segment(uuid=UUID_B))
        replacement.replace(path)
        result = self.observe(path.name)
        self.assertNotEqual(result.checkpoint.key, first.checkpoint.key)
        self.assertEqual(result.resume_offset, 0)
        self.assertIn("source_gap", result.diagnostics)
        self.assertEqual(len(self.tracker.checkpoints), 2)

    def test_truncate_preserves_old_position_and_starts_generation(self):
        path = self.write("same.open.jsonl", segment(b'{"event":1}\n' * 20))
        self.observe(path.name, commit=True)
        old = self.tracker.checkpoints[0]
        path.write_bytes(segment())
        result = self.observe(path.name)
        self.assertEqual(result.checkpoint.generation, 1)
        self.assertEqual(result.resume_offset, 0)
        self.assertIn("source_truncated", result.diagnostics)
        self.assertEqual(self.tracker.checkpoints[0], old)

    def test_truncate_regrow_changes_anchor_even_with_reused_inode_hint(self):
        original = segment(b"A" * 100 + b"\n")
        path = self.write("same.open.jsonl", original)
        self.observe(path.name, commit=True)
        old = self.tracker.checkpoints[0]
        # Replace the descriptor but simulate inode reuse in the same mount view.
        replacement = self.write("replacement", segment(b"B" * 200 + b"\n"))
        replacement.replace(path)
        real_fstat = os.fstat

        def reused(fd):
            values = list(real_fstat(fd))
            values[1], values[2] = old.inode, old.device
            return os.stat_result(values)

        with patch("sledra_analysis.ingest.source.os.fstat", side_effect=reused):
            result = self.observe(path.name)
        self.assertEqual(result.checkpoint.generation, 1)
        self.assertEqual(result.resume_offset, 0)
        self.assertIn("source_rewritten", result.diagnostics)

    def test_uncommitted_tail_rewrite_is_detected(self):
        path = self.write("same.open.jsonl", segment(b"A" * 100 + b"\n"))
        self.observe(path.name)
        path.write_bytes(segment(b"B" * 100 + b"\n"))
        result = self.observe(path.name)
        self.assertEqual(result.checkpoint.generation, 1)
        self.assertIn("source_rewritten", result.diagnostics)

    def test_sealed_duplicate_copy_ignores_inode(self):
        path = self.write("a.sealed.jsonl", segment())
        os.link(path, self.root / "a.open.jsonl")
        self.observe("a.sealed.jsonl", commit=True)
        self.write("b.sealed.jsonl", segment())
        result = self.observe("b.sealed.jsonl")
        self.assertEqual(result.checkpoint.generation, 0)
        self.assertEqual(result.resume_offset, len(segment()))
        self.assertEqual(len(self.tracker.checkpoints), 1)
        # Discovery order across copies must not turn the original open alias
        # into a false conflict merely because the latest inode hint was b.
        alias = self.observe("a.open.jsonl")
        self.assertEqual(alias.checkpoint.generation, 0)
        self.assertEqual(alias.resume_offset, len(segment()))

    def test_equal_length_legacy_anchor_rewrite_starts_a_generation(self):
        path = self.write("legacy.log", b"first\nsecond\n")
        self.observe(path.name, commit=True, legacy="old-node")
        old = self.tracker.checkpoints[0]
        path.write_bytes(b"first\nSECOND\n")
        result = self.observe(path.name, legacy="old-node")
        self.assertEqual(result.checkpoint.generation, 1)
        self.assertEqual(result.resume_offset, 0)
        self.assertIn("source_gap", result.diagnostics)
        self.assertIn("legacy_source_integrity_unproven", result.diagnostics)
        self.assertEqual(self.tracker.checkpoints[0], old)

    def test_short_pread_is_retried_and_eof_does_not_advance(self):
        self.write("a.open.jsonl", segment())
        with self.tracker.open("a.open.jsonl") as lease:
            real_pread = os.pread

            def partial(fd, length, offset):
                return real_pread(fd, min(length, 7), offset)

            with patch("sledra_analysis.ingest.source.os.pread", side_effect=partial):
                self.assertEqual(lease.read(0, len(segment())), segment())
            with patch("sledra_analysis.ingest.source.os.pread", return_value=b""):
                with self.assertRaises(SourceChanged):
                    lease.read(0, 100)
                with self.assertRaises(SourceChanged):
                    lease.checkpoint(len(segment()))
        self.assertEqual(self.tracker.checkpoints[0].committed_offset, 0)

    def test_sealed_uuid_conflict_retains_versions_and_blocks_both_after_restart(self):
        self.write("a.sealed.jsonl", segment())
        self.observe("a.sealed.jsonl", commit=True)
        self.write("b.sealed.jsonl", segment(b'{"event":2}\n'))
        result = self.observe("b.sealed.jsonl")
        self.assertEqual(result.action, "conflict")
        self.assertIsNone(result.resume_offset)
        self.assertEqual(len(self.tracker.checkpoints), 2)
        self.assertEqual(self.tracker.checkpoints[0].committed_offset, len(segment()))
        self.assertEqual(self.tracker.checkpoints[0].discovered_paths, ("a.sealed.jsonl",))
        self.assertEqual(self.tracker.checkpoints[1].discovered_paths, ("b.sealed.jsonl",))
        self.assertTrue(all(state.conflict for state in self.tracker.checkpoints))
        self.tracker = SourceTracker(self.root, "root-1", self.tracker.checkpoints)
        for name in ("a.sealed.jsonl", "b.sealed.jsonl"):
            with self.tracker.open(name) as lease:
                self.assertIsNone(lease.decision.resume_offset)
                with self.assertRaises(SourceError):
                    lease.read(0, 100)
                with self.assertRaises(SourceError):
                    lease.checkpoint(0)

    def test_sealed_change_outside_sample_windows_uses_full_digest(self):
        raw = segment(b"A" * (WINDOW * 4) + b"\n")
        path = self.write("a.sealed.jsonl", raw)
        self.observe(path.name, commit=True)
        with path.open("r+b") as stream:
            stream.seek(WINDOW * 2)
            stream.write(b"B")
        result = self.observe(path.name)
        self.assertEqual(result.action, "conflict")
        self.assertIn("segment_protocol_violation", result.diagnostics)

    def test_same_uuid_header_conflict(self):
        path = self.write("a.open.jsonl", segment())
        self.observe(path.name)
        path.write_bytes(segment().replace(b"test-session", b"evil-session"))
        result = self.observe(path.name)
        self.assertEqual(result.action, "conflict")
        self.assertIsNone(result.resume_offset)

    def test_source_and_root_namespaces_are_distinct(self):
        path = self.write("a.open.jsonl", segment())
        a = self.observe(path.name)
        self.write("b.open.jsonl", segment().replace(b"test-node", b"other-node"))
        b = self.observe("b.open.jsonl")
        self.assertNotEqual(a.checkpoint.key, b.checkpoint.key)
        with self.assertRaises(SourceError):
            SourceTracker(self.root, "other-root", (a.checkpoint,))

    def test_legacy_append_rename_replacement_and_truncation(self):
        path = self.write("legacy.log", b"first\n")
        self.observe(path.name, commit=True, legacy="legacy-node")
        with path.open("ab") as stream:
            stream.write(b"second\n")
        appended = self.observe(path.name, legacy="legacy-node")
        self.assertEqual(appended.resume_offset, 6)
        path.rename(self.root / "legacy.1")
        renamed = self.observe("legacy.1", legacy="legacy-node")
        self.assertEqual(renamed.checkpoint.key, appended.checkpoint.key)
        self.assertEqual(renamed.resume_offset, 6)
        self.write("legacy.1", b"x\n")
        truncated = self.observe("legacy.1", legacy="legacy-node")
        self.assertEqual(truncated.resume_offset, 0)
        self.assertEqual(truncated.checkpoint.generation, 1)
        self.assertIn("legacy_source_integrity_unproven", truncated.diagnostics)
        self.write("legacy.log", b"new\n")
        replaced = self.observe("legacy.log", legacy="legacy-node")
        self.assertEqual(replaced.checkpoint.generation, 2)
        self.assertEqual(replaced.resume_offset, 0)

    def test_legacy_same_length_unsampled_rewrite_is_explicitly_unproven(self):
        path = self.write("legacy.log", b"A" * (WINDOW * 5) + b"\n")
        self.observe(path.name, commit=True, legacy="legacy-node")
        with path.open("r+b") as stream:
            stream.seek(WINDOW * 2)
            stream.write(b"B" * WINDOW)
        result = self.observe(path.name, legacy="legacy-node")
        # Equal-length changes outside all stored windows cannot be distinguished
        # from a harmless metadata update. Never report this source as proven intact.
        self.assertEqual(result.checkpoint.generation, 0)
        self.assertIn("legacy_source_integrity_unproven", result.diagnostics)

    def test_partial_tail_does_not_allow_position_advance(self):
        complete = segment()
        path = self.write("a.open.jsonl", complete + b"partial")
        with self.tracker.open(path.name) as lease:
            with self.assertRaises(SourceError):
                lease.checkpoint(path.stat().st_size)
            position = lease.checkpoint(len(complete))
            self.assertEqual(position.committed_offset, len(complete))
        path.rename(self.root / "a.sealed.jsonl")
        sealed = self.observe("a.sealed.jsonl")
        self.assertIn("sealed_incomplete_tail", sealed.diagnostics)

    def test_changed_read_and_scan_never_install_a_position(self):
        path = self.write("a.open.jsonl", segment())
        with self.tracker.open(path.name) as lease:
            path.write_bytes(segment(b'{"event":2}\n'))
            with self.assertRaises(SourceChanged):
                lease.read(0, 100)
            with self.assertRaises(SourceChanged):
                lease.checkpoint(len(segment()))
        old = self.tracker.checkpoints
        original_inspect = self.tracker._inspect

        def mutate(*args):
            result = original_inspect(*args)
            with path.open("ab") as stream:
                stream.write(b"changed\n")
            return result

        with patch.object(self.tracker, "_inspect", side_effect=mutate):
            with self.assertRaises(SourceChanged):
                self.observe(path.name)
        self.assertEqual(self.tracker.checkpoints, old)

    def test_paths_and_headers_fail_closed(self):
        self.write("a.open.jsonl", segment())
        (self.root / "alias").symlink_to(self.root / "a.open.jsonl")
        (self.root / "dir-alias").symlink_to(self.root, target_is_directory=True)
        os.mkfifo(self.root / "fifo")
        for locator in (
            "../a.open.jsonl",
            str(self.root / "a.open.jsonl"),
            "alias",
            "dir-alias/a.open.jsonl",
            "fifo",
        ):
            with self.subTest(locator=locator), self.assertRaises((OSError, SourceError)):
                self.observe(locator)
        for raw in (
            b"legacy\n",
            segment().split(b"\n")[0],
            b"X" * 65537 + b"\n",
            segment().replace(b"1.0.0", b"2.0.0"),
            segment().replace(b'"source_id":', b'"source_id":"x","source_id":'),
            segment().replace(UUID_A.encode(), b"invalid-uuid"),
        ):
            self.write("bad.open.jsonl", raw)
            with self.subTest(raw=raw[:40]), self.assertRaises(SourceError):
                self.observe("bad.open.jsonl")
        self.assertEqual(self.tracker.checkpoints, ())

    def test_frozen_handoff_identity_and_offsets(self):
        fixture = ROOT / "tests/m1/results/handoff.json"
        self.assertEqual(
            hashlib.sha256(fixture.read_bytes()).hexdigest(),
            "5c6c2bf40af052eedaf0edeb4ea02119f6d899824e086c8e26e210a436e282ef",
        )
        handoff = json.loads(fixture.read_bytes())
        sources, events = [], 0
        for item in handoff["files"]:
            raw = item["text"].encode()
            self.assertEqual(hashlib.sha256(raw).hexdigest(), item["sha256"])
            if not item["path"].endswith(".jsonl"):
                continue
            self.write(item["path"], raw)
            with self.tracker.open(item["path"]) as lease:
                decision = lease.decision
                self.assertEqual(decision.resume_offset, 0)
                self.assertEqual(lease.read(0, len(raw)), raw)
                header = json.loads(raw.split(b"\n")[0])
                self.assertEqual(decision.checkpoint.key.segment_uuid, header["segment_uuid"])
                self.tracker.remember(lease.checkpoint(len(raw)))
                sources.append(decision.checkpoint.key)
                events += len(raw.splitlines()) - 1
            resumed = self.observe(item["path"])
            self.assertEqual(resumed.resume_offset, len(raw))
        self.assertEqual(len(set(sources)), 6)
        self.assertEqual(events, 11)


if __name__ == "__main__":
    unittest.main(verbosity=2)
