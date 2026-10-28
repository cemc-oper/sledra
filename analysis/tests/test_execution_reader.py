"""Read-only scans of synthetic files, including broken/active history and Runs."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
import uuid
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch

from sledra_analysis.execution.codec import ContractError, execution_path, partition_date
from sledra_analysis.execution.reader import Fact, FileReader, ReadIssue
from sledra_analysis.execution import reader as reader_module

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "spec/execution-v2/cases"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


class ExecutionReaderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sledra direct reader ")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "records"
        self.root.mkdir()
        self.config = json.loads((CASES / "root.json").read_bytes())
        (self.root / "sledra.json").write_bytes(encoded(self.config))

    def event(
        self, identity=None, name="execution.started", when="2026-10-27T23:59:59+08:00", attrs=None
    ):
        return {
            "format_version": "2.0.0",
            "event_id": str(uuid.uuid4()),
            "execution_id": identity or str(uuid.uuid4()),
            "name": name,
            "occurred_at": when,
            "attributes": attrs
            if attrs is not None
            else {"task": "synthetic.forecast", "engine": None},
        }

    def put_events(self, events, raw=None):
        first = events[0]
        path = self.root / execution_path(first["execution_id"], first["occurred_at"], self.config)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw if raw is not None else b"".join(encoded(e) + b"\n" for e in events))
        return path

    def put_run(self, *, record=None, identity=None, when="2026-10-27T23:59:59+08:00"):
        record = copy.deepcopy(record or json.loads((CASES / "run-single.json").read_bytes()))
        if identity is not None:
            record["run_id"] = identity
        record["created_at"] = when
        path = self.root / "runs" / partition_date(when, self.config) / (record["run_id"] + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded(record))
        return path, record

    def snapshot(self):
        return {
            str(p.relative_to(self.root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in self.root.rglob("*")
            if p.is_file() and not p.is_symlink()
        }

    def test_window_entry_date_cross_midnight_and_positions(self):
        start = self.event()
        context = self.event(
            start["execution_id"],
            "execution.context",
            "2026-10-28T08:00:00+08:00",
            {
                "task": "forecast",
                "parameters": {"resolution_km": 25},
                "scope": None,
                "cycle_kind": "single",
                "cycle": "2024-07-01T00:00:00+08:00",
                "run_id": None,
            },
        )
        end = self.event(
            start["execution_id"], "execution.ended", "2026-10-28T09:00:00+08:00", {"exit_code": 7}
        )
        path = self.put_events([start, context, end])
        another = self.event(when="2026-10-28T00:00:00+08:00")
        self.put_events([another])
        before = self.snapshot()
        with FileReader(self.root) as reader:
            rows = list(reader.iter_events("2026-10-27", "2026-10-27"))
            self.assertEqual([r.data for r in rows], [start, context, end])
            offset = 0
            for line, item in enumerate(rows, 1):
                self.assertIsInstance(item, Fact)
                self.assertEqual(
                    (item.source.path, item.source.line, item.source.offset, item.source.length),
                    (str(path.relative_to(self.root)), line, offset, len(encoded(item.data)) + 1),
                )
                offset += item.source.length
            self.assertEqual(
                len(list(reader.iter_events(dt.date(2026, 10, 28), dt.date(2026, 10, 28)))), 1
            )
            self.assertEqual(len(list(reader.iter_events(None, None))), 4)
            cfg = reader.config
            cfg["partition_timezone"] = "-14:00"
            self.assertEqual(reader.config, self.config)
        self.assertEqual(self.snapshot(), before)

    def test_invalid_window_and_missing_empty_history_never_create_paths(self):
        with FileReader(self.root) as reader:
            self.assertEqual(list(reader.iter_events("2026-10-27", "2026-10-28")), [])
            self.assertEqual(list(reader.iter_runs()), [])
            for start, end in [
                ("20261027", "2026-10-28"),
                ("2026-10-29", "2026-10-28"),
                (None, "2026-10-28"),
                ("0000-01-01", "2026-10-28"),
            ]:
                with self.assertRaises(ContractError) as caught:
                    list(reader.iter_events(start, end))
                self.assertEqual(caught.exception.code, "invalid_window")
        self.assertEqual(list(self.root.iterdir()), [self.root / "sledra.json"])
        reader.close()
        with self.assertRaisesRegex(ContractError, "reader_closed"):
            list(reader.iter_events(None, None))

    def test_record_byte_boundary_includes_exactly_one_separate_lf(self):
        start = self.event()
        raw = encoded(start)
        exact = raw + b" " * (65536 - len(raw))
        self.put_events([start], exact + b"\n" + exact + b" \n" + raw + b"\n")
        with FileReader(self.root) as reader:
            rows = list(reader.iter_events(None, None))
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].source.length, 65537)
        self.assertEqual(rows[1].code, "limit_exceeded")
        self.assertEqual(rows[1].source.length, 65538)
        self.assertEqual(rows[2].data, start)

    def test_explicit_date_window_never_opens_other_date_directories(self):
        start = self.event()
        path = self.put_events([start])
        outside = self.root / "executions/20261028"
        outside.symlink_to(self.base, target_is_directory=True)
        (self.root / "executions/not-a-date").mkdir()
        (path.parent / "not-an-execution.jsonl").write_bytes(b"not read as an event")
        (path.parent.parent / "z").mkdir()
        with FileReader(self.root) as reader:
            rows = list(reader.iter_events("2026-10-27", "2026-10-27"))
            self.assertEqual([r.data for r in rows if isinstance(r, Fact)], [start])
            self.assertEqual(
                [r.code for r in rows if isinstance(r, ReadIssue)], ["invalid_layout"] * 3
            )
            self.assertFalse(any(r.source.path == "executions/20261028" for r in rows))
            all_rows = list(reader.iter_events(None, None))
            self.assertTrue(
                any(
                    isinstance(r, ReadIssue) and r.source.path == "executions/20261028"
                    for r in all_rows
                )
            )

    def test_bad_rows_are_isolated_and_oversized_lines_are_bounded(self):
        start = self.event()
        end = self.event(start["execution_id"], "execution.ended", attrs={"exit_code": 0})
        huge = b"x" * (4 * 1024 * 1024) + b"\n"
        raw = (
            encoded(start)
            + b"\n"
            + b'{"bad":}\n'
            + b"\xff\n"
            + b"\n"
            + encoded(end)
            + b"\r\n"
            + huge
            + encoded(end)
            + b"\n"
        )
        path = self.put_events([start], raw)
        another = self.event()
        self.put_events([another])
        with FileReader(self.root) as reader:
            tracemalloc.start()
            rows = list(reader.iter_events(None, None))
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        self.assertLess(peak, 2 * 1024 * 1024)
        facts = [r for r in rows if isinstance(r, Fact)]
        issues = [r for r in rows if isinstance(r, ReadIssue)]
        self.assertEqual(len(facts), 3)
        self.assertEqual(
            {r.data["execution_id"] for r in facts},
            {start["execution_id"], another["execution_id"]},
        )
        self.assertEqual(
            {r.code for r in issues}, {"invalid_json", "invalid_unicode", "limit_exceeded"}
        )
        self.assertEqual(len(issues), 5)
        oversized = next(r for r in issues if r.code == "limit_exceeded")
        self.assertEqual(
            (oversized.source.path, oversized.source.line, oversized.source.length),
            (str(path.relative_to(self.root)), 6, len(huge)),
        )
        self.assertEqual(path.read_bytes(), raw)

    def test_active_partial_tail_is_only_read_after_lf_on_a_later_scan(self):
        start = self.event()
        end = self.event(start["execution_id"], "execution.ended", attrs={"exit_code": 0})
        endraw = encoded(end)
        path = self.put_events([start], encoded(start) + b"\n" + endraw[:30])
        with FileReader(self.root) as reader:
            first = list(reader.iter_events(None, None))
            self.assertEqual(len(first), 2)
            self.assertEqual(first[-1].code, "incomplete_tail")
            self.assertEqual(
                (first[-1].source.line, first[-1].source.offset, first[-1].source.length),
                (2, len(encoded(start)) + 1, 30),
            )
            with path.open("ab") as out:
                out.write(endraw[30:] + b"\n")
            second = list(reader.iter_events(None, None))
            self.assertEqual([r.data for r in second], [start, end])

    def test_file_size_snapshot_excludes_later_appends_and_reports_truncation(self):
        start = self.event()
        end = self.event(start["execution_id"], "execution.ended", attrs={"exit_code": 0})
        path = self.put_events([start])
        with FileReader(self.root) as reader:
            scan = reader.iter_events(None, None)
            self.assertEqual(next(scan).data, start)
            with path.open("ab") as out:
                out.write(encoded(end) + b"\n")
            self.assertEqual(list(scan), [])
            self.assertEqual(len(list(reader.iter_events(None, None))), 2)
        # More than the read buffer remains when the file is truncated.
        self.put_events([start], encoded(start) + b"\n" + (b"x" * 65536 + b"\n") * 3)
        with FileReader(self.root) as reader:
            scan = reader.iter_events(None, None)
            next(scan)
            path.write_bytes(b"")
            rest = list(scan)
            self.assertIn("source_changed", [r.code for r in rest if isinstance(r, ReadIssue)])

    def test_identity_bucket_and_start_date_mismatches_do_not_hide_other_files(self):
        start = self.event()
        foreign = self.event(name="example.progress", attrs={})
        path = self.put_events([start], encoded(start) + b"\n" + encoded(foreign) + b"\n")
        other = self.event()
        otherpath = self.put_events([other])
        # Put a validly framed start in the wrong record date.
        wrong = self.event(when="2026-10-28T00:00:00+08:00")
        wrongpath = self.put_events([wrong])
        new = wrongpath.parent.parent.parent / "20261029" / wrongpath.parent.name / wrongpath.name
        new.parent.mkdir(parents=True, exist_ok=True)
        wrongpath.rename(new)
        bucket = self.event()
        badbucket = self.put_events([bucket])
        target = (
            badbucket.parent.parent
            / ("0" if badbucket.parent.name != "0" else "1")
            / badbucket.name
        )
        target.parent.mkdir(exist_ok=True)
        badbucket.rename(target)
        with FileReader(self.root) as reader:
            rows = list(reader.iter_events(None, None))
        self.assertEqual(
            {r.code for r in rows if isinstance(r, ReadIssue)},
            {"execution_mismatch", "partition_mismatch", "bucket_mismatch"},
        )
        self.assertEqual(
            {r.source.path for r in rows if isinstance(r, Fact)},
            {str(path.relative_to(self.root)), str(otherpath.relative_to(self.root))},
        )

    def test_conflicting_and_retransmitted_events_are_retained_for_p2_02(self):
        start = self.event()
        context = self.event(
            start["execution_id"],
            "execution.context",
            attrs={
                "task": "forecast",
                "parameters": {},
                "scope": None,
                "cycle_kind": "none",
                "cycle": None,
                "run_id": None,
            },
        )
        changed = copy.deepcopy(context)
        changed["attributes"]["task"] = "other"
        self.put_events([start, context, context, changed])
        with FileReader(self.root) as reader:
            rows = list(reader.iter_events(None, None))
        self.assertEqual(len(rows), 4)
        self.assertTrue(all(isinstance(r, Fact) for r in rows))
        self.assertEqual(rows[1].data, rows[2].data)
        self.assertNotEqual(rows[2].data, rows[3].data)

    def test_runs_scan_creation_dates_independently_and_missing_references_stay_visible(self):
        path, run = self.put_run(when="2024-01-01T00:00:00+08:00")
        start = self.event()
        context = self.event(
            start["execution_id"],
            "execution.context",
            attrs={
                "task": "forecast",
                "parameters": {"resolution_km": 25},
                "scope": "forecast",
                "cycle_kind": "single",
                "cycle": run["cycle"],
                "run_id": run["run_id"],
            },
        )
        self.put_events([start, context])
        missing = str(uuid.uuid4())
        before = self.snapshot()
        with FileReader(self.root) as reader:
            self.assertEqual(len(list(reader.iter_events("2026-10-27", "2026-10-27"))), 2)
            lookup = reader.resolve_runs([run["run_id"], missing])
            self.assertEqual(lookup.issues, ())
            self.assertEqual(lookup.references[run["run_id"]].status, "found")
            self.assertEqual(
                lookup.references[run["run_id"]].records[0].source.path,
                str(path.relative_to(self.root)),
            )
            self.assertEqual(lookup.references[missing].status, "missing")
            path.unlink()
            self.assertEqual(
                reader.resolve_runs([run["run_id"]]).references[run["run_id"]].status, "missing"
            )
            self.assertEqual(len(list(reader.iter_events(None, None))), 2)
        # Only the deliberate synthetic Run deletion changed bytes.
        del before[str(path.relative_to(self.root))]
        self.assertEqual(self.snapshot(), before)

    def test_run_conflict_bad_namespace_invalid_file_and_missing_are_distinct(self):
        _, run = self.put_run()
        self.put_run(identity=run["run_id"], when="2026-10-28T00:00:00+08:00")
        wrong = copy.deepcopy(run)
        wrong["run_id"] = str(uuid.uuid4())
        wrong["namespace"]["deployment"] = "foreign"
        self.put_run(record=wrong)
        badpath, bad = self.put_run(identity=str(uuid.uuid4()))
        badpath.write_bytes(b"invalid")
        goodpath, good = self.put_run(identity=str(uuid.uuid4()))
        missing = str(uuid.uuid4())
        with FileReader(self.root) as reader:
            lookup = reader.resolve_runs([r["run_id"] for r in (run, wrong, bad, good)] + [missing])
        self.assertEqual(
            {i: r.status for i, r in lookup.references.items()},
            {
                run["run_id"]: "conflicting",
                wrong["run_id"]: "unavailable",
                bad["run_id"]: "unavailable",
                good["run_id"]: "found",
                missing: "missing",
            },
        )
        self.assertEqual(len(lookup.references[run["run_id"]].records), 2)
        self.assertEqual({r.code for r in lookup.issues}, {"namespace_mismatch", "invalid_json"})
        self.assertTrue(goodpath.exists())

    def test_run_scan_failure_cannot_claim_missing_or_a_unique_winner(self):
        _, run = self.put_run()
        missing = str(uuid.uuid4())
        (self.root / "runs/20261028").symlink_to(self.base, target_is_directory=True)
        with FileReader(self.root) as reader:
            lookup = reader.resolve_runs([run["run_id"], missing])
        self.assertEqual({r.status for r in lookup.references.values()}, {"unavailable"})
        self.assertEqual(len(lookup.references[run["run_id"]].records), 1)

    def test_run_path_binding_size_and_empty_tail_errors_retain_other_records(self):
        path, record = self.put_run()
        wrong_id = str(uuid.uuid4())
        wrong_name = path.with_name(wrong_id + ".json")
        path.rename(wrong_name)
        other, good = self.put_run(identity=str(uuid.uuid4()))
        dated, bad_date = self.put_run(identity=str(uuid.uuid4()))
        moved = self.root / "runs/20261029" / dated.name
        moved.parent.mkdir()
        dated.rename(moved)
        huge, oversized = self.put_run(identity=str(uuid.uuid4()))
        huge.write_bytes(b" " * (1024 * 1024))
        empty = self.put_events([self.event()], b"")
        with FileReader(self.root) as reader:
            rows = list(reader.iter_runs())
            events = list(reader.iter_events(None, None))
        self.assertEqual([r.data for r in rows if isinstance(r, Fact)], [good])
        self.assertEqual(
            {r.code for r in rows if isinstance(r, ReadIssue)},
            {"run_mismatch", "partition_mismatch", "limit_exceeded"},
        )
        self.assertEqual(
            next(
                r.source.length
                for r in rows
                if isinstance(r, ReadIssue) and r.code == "limit_exceeded"
            ),
            1024 * 1024,
        )
        self.assertEqual(
            [(r.code, r.source.line, r.source.length) for r in events], [("incomplete_tail", 1, 0)]
        )
        self.assertEqual(empty.read_bytes(), b"")
        original = reader_module._regular

        @contextmanager
        def short_read(parent, name):
            with original(parent, name) as (file, size):
                yield file, size + 1

        with FileReader(self.root) as reader:
            with patch.object(reader_module, "_regular", short_read):
                truncated = list(reader.iter_runs([good["run_id"]]))
        self.assertEqual([r.code for r in truncated], ["source_changed"])

    def test_symlinks_hardlinks_fifo_and_unreadable_buckets_are_not_followed(self):
        first = self.event()
        path = self.put_events([first])
        outside = self.base / "outside"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        second = self.event()
        hard = self.put_events([second])
        os.link(hard, self.base / "hardlink")
        third = self.event()
        fifo = self.put_events([third])
        fifo.unlink()
        os.mkfifo(fifo)
        fourth = self.event()
        good = self.put_events([fourth])
        with FileReader(self.root) as reader:
            rows = list(reader.iter_events(None, None))
        self.assertEqual(
            {r.data["execution_id"] for r in rows if isinstance(r, Fact)}, {fourth["execution_id"]}
        )
        self.assertEqual(len([r for r in rows if isinstance(r, ReadIssue)]), 3)
        if os.geteuid() != 0:
            # A failed bucket listing must not abort other buckets in that date.
            denied = self.root / "executions/20261027" / ("f" if good.parent.name != "f" else "e")
            denied.mkdir(exist_ok=True)
            denied.chmod(0o000)
            try:
                with FileReader(self.root) as reader:
                    rows = list(reader.iter_events(None, None))
                self.assertTrue(
                    any(
                        isinstance(r, Fact) and r.data["execution_id"] == fourth["execution_id"]
                        for r in rows
                    )
                )
                self.assertTrue(
                    any(
                        isinstance(r, ReadIssue)
                        and r.source.path == str(denied.relative_to(self.root))
                        for r in rows
                    )
                )
            finally:
                denied.chmod(0o700)
        self.assertEqual(outside.read_bytes(), encoded(first) + b"\n")

    def test_root_is_read_only_compatible_and_rejects_all_symlink_ancestors(self):
        for mutate in ["missing", "old", "malformed", "marker_link", "ancestor_link"]:
            with self.subTest(mutate=mutate):
                base = self.base / mutate
                base.mkdir()
                root = base / "root"
                root.mkdir()
                (root / "sledra.json").write_bytes(encoded(self.config))
                if mutate == "missing":
                    (root / "sledra.json").unlink()
                elif mutate == "old":
                    (root / "registry").mkdir()
                elif mutate == "malformed":
                    (root / "sledra.json").write_bytes(b"{}")
                elif mutate == "marker_link":
                    (root / "sledra.json").unlink()
                    (root / "sledra.json").symlink_to(self.root / "sledra.json")
                else:
                    alias = base / "alias"
                    alias.symlink_to(root, target_is_directory=True)
                    root = alias
                with self.assertRaises(ContractError):
                    FileReader(root)
        with self.assertRaisesRegex(ContractError, "root_invalid"):
            FileReader("relative")
        with FileReader(self.root) as reader:
            self.assertEqual(reader.config, self.config)
        alias = self.base / "ancestor-alias"
        alias.symlink_to(self.base, target_is_directory=True)
        with self.assertRaisesRegex(ContractError, "root_unavailable"):
            FileReader(alias / "records")
        missing = self.base / "must-not-create"
        with self.assertRaises(ContractError):
            FileReader(missing)
        self.assertFalse(missing.exists())

    def test_fresh_process_rejects_fifo_without_storage_network_or_write_imports(self):
        path = self.put_events([self.event()])
        path.unlink()
        os.mkfifo(path)
        child = r"""
import os, sys
sys.path.insert(0,sys.argv[1])
def audit(event, args):
    if event == "socket.connect" or event.startswith("sqlite3.connect"):
        raise AssertionError(event)
    if event == "open" and args[2] & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND):
        raise AssertionError("unexpected write")
sys.addaudithook(audit)
import sledra_analysis.execution
assert "jsonschema" not in sys.modules
from sledra_analysis.execution.reader import FileReader,ReadIssue
with FileReader(sys.argv[2]) as reader:
    rows=list(reader.iter_events(None,None))
assert len(rows)==1 and isinstance(rows[0],ReadIssue) and rows[0].code=="input_unavailable"
assert not any(n=="sqlite3" or n.startswith(("sledra_analysis.ingest","sledra_analysis.store","sledra_analysis.projections","sledra_analysis.metrics")) for n in sys.modules)
print("independent read-only FIFO scan passed")
"""
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", child, str(ROOT / "analysis/src"), str(self.root)],
            cwd=self.base,
            capture_output=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, b"independent read-only FIFO scan passed\n")


if __name__ == "__main__":
    unittest.main(verbosity=2)
