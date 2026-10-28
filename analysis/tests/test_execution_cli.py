"""Direct history CLI over temporary synthetic files; no scheduler or database."""

from __future__ import annotations

import contextlib
import datetime as dt
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from sledra_analysis.execution import cli
from sledra_analysis.execution.codec import execution_path, partition_date

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "spec/execution-v2/cases"
NOW = dt.datetime(2026, 10, 28, 0, 30, tzinfo=dt.timezone.utc)
WHEN = "2026-10-28T08:30:00+08:00"
CYCLE = "2024-07-01T00:00:00+08:00"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


class HistoryCLI(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sledra cli synthetic ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "records"
        self.root.mkdir()
        self.config = json.loads((CASES / "root.json").read_bytes())
        (self.root / "sledra.json").write_bytes(encoded(self.config))

    def event(self, identity, name, attrs, when=WHEN):
        return {
            "format_version": "2.0.0",
            "event_id": str(uuid.uuid4()),
            "name": name,
            "occurred_at": when,
            "execution_id": identity,
            "attributes": attrs,
        }

    def records(
        self,
        *,
        when=WHEN,
        cycle=CYCLE,
        kind="single",
        started=True,
        ended=True,
        parameters=None,
        run=None,
        task="main.forecast",
        scope=None,
        identity=None,
    ):
        identity = identity or str(uuid.uuid4())
        result = []
        if started:
            result.append(
                self.event(identity, "execution.started", {"task": "entry", "engine": None}, when)
            )
        result.append(
            self.event(
                identity,
                "execution.context",
                {
                    "task": task,
                    "parameters": parameters if parameters is not None else {"resolution_km": 25},
                    "scope": scope,
                    "cycle_kind": kind,
                    "cycle": cycle,
                    "run_id": run,
                },
                when,
            )
        )
        if ended:
            result.append(self.event(identity, "execution.ended", {"exit_code": 0}, when))
        return result

    def put(self, records, *, raw=None, entry=None):
        path = self.root / execution_path(
            records[0]["execution_id"], entry or records[0]["occurred_at"], self.config
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw if raw is not None else b"".join(encoded(e) + b"\n" for e in records))
        return path

    def run_cli(self, *arguments, now=NOW):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                args = cli._arguments(["--root", str(self.root), *arguments])
                with cli.FileReader(args.root) as reader:
                    code = cli._query(reader, args, now)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def json_cli(self, *arguments):
        code, out, err = self.run_cli("--format", "jsonl", *arguments)
        self.assertEqual(err, "")
        rows = [json.loads(line, parse_float=Decimal) for line in out.split("\n") if line]
        return code, rows

    def test_default_seven_days_uses_root_offset_and_always_reports_empty_scope(self):
        for zone, first, last in (
            ("+08:00", "2026-10-22", "2026-10-28"),
            ("-08:00", "2026-10-21", "2026-10-27"),
            ("+14:00", "2026-10-22", "2026-10-28"),
        ):
            self.config["partition_timezone"] = zone
            (self.root / "sledra.json").write_bytes(encoded(self.config))
            code, rows = self.json_cli()
            self.assertEqual(code, 0)
            self.assertEqual((rows[0]["record_from"], rows[0]["record_through"]), (first, last))
            self.assertEqual(rows[0]["mode"], "default_seven_days")
            self.assertTrue(rows[-1]["no_matches"])
            self.assertTrue(rows[-1]["coverage"]["finite_window"])
        code, text, _ = self.run_cli()
        self.assertEqual(code, 0)
        self.assertIn("Coverage:", text)
        self.assertIn("finite window is not all history", text)
        self.assertIn('"no_matches":true', text)

    def test_old_cycle_is_found_in_today_partition_and_id_does_not_expand_window(self):
        records = self.records()
        self.put(records)
        identity = records[0]["execution_id"]
        code, rows = self.json_cli("--cycle", "2024-06-30T16:00:00Z")
        self.assertEqual(code, 0)
        details = [r for r in rows if r["type"] == "execution"]
        self.assertEqual(len(details), 1)
        self.assertEqual(details[0]["cycle_display"], "2024-07-01T00:00:00.000000000+08:00")
        code, rows = self.json_cli(
            "--execution-id",
            identity,
            "--record-from",
            "2024-07-01",
            "--record-through",
            "2024-07-01",
        )
        self.assertEqual(code, 0)
        self.assertEqual(rows[-1]["matched"]["visible"], 0)
        self.assertEqual(rows[-1]["coverage"]["record_from"], "2024-07-01")
        code, rows = self.json_cli("--execution-id", identity, "--all-record-dates")
        self.assertEqual(code, 0)
        self.assertEqual(rows[-1]["matched"]["started"], 1)
        self.assertEqual(rows[0]["mode"], "all_retained")
        self.assertIsNone(rows[0]["record_from"])

    def test_inclusive_window_filters_record_dates_and_not_event_end_dates(self):
        first = self.records(when="2026-10-21T23:59:59+08:00")
        first[-1]["occurred_at"] = "2026-10-28T08:30:00+08:00"
        self.put(first)
        self.put(self.records(when="2026-10-22T00:00:00+08:00"))
        self.put(self.records(when="2026-10-29T00:00:00+08:00"))
        _, rows = self.json_cli()
        self.assertEqual(rows[-1]["matched"]["visible"], 1)
        _, rows = self.json_cli("--record-from", "2026-10-21", "--record-through", "2026-10-22")
        self.assertEqual(rows[-1]["matched"]["visible"], 2)
        _, rows = self.json_cli("--all-record-dates")
        self.assertEqual(rows[-1]["matched"]["visible"], 3)

    def test_arguments_reject_incompatible_or_unbounded_inputs_before_scanning(self):
        invalid = [
            ["--record-from", "2026-10-28"],
            ["--record-through", "2026-10-28"],
            ["--record-from", "2026-10-29", "--record-through", "2026-10-28"],
            ["--all-record-dates", "--record-from", "2026-10-28"],
            ["--record-from", "20261028", "--record-through", "2026-10-28"],
            ["--record-from", "2026-02-30", "--record-through", "2026-10-28"],
            ["--cycle", "2024-07-01"],
            ["--cycle", "2024-07-01T00:00:00-00:00"],
            ["--cycle", CYCLE, "--cycle-kind", "none"],
            ["--execution-id", "not-id"],
            ["--run-id", "00000000-0000-0000-0000-000000000001"],
            ["--parameter", "bad-key=1"],
            ["--parameter", "key=[]"],
            ["--parameter", "key={}"],
            ["--parameter", "key=NaN"],
            ["--parameter", "key=1e309"],
            ["--parameter", "key=9223372036854775808"],
            ["--parameter", "key=1", "--parameter", "key=2"],
            ["--parameter", 'key="\\ud800"'],
            ["--group-by", "task,task"],
            ["--group-by", "attempt"],
            ["--group-by", ""],
            ["--event", "execution.started"],
            ["--event", "bad"],
            ["--rec", "2026-10-28"],
        ]
        with patch.object(cli, "FileReader", side_effect=AssertionError("should not open root")):
            for args in invalid:
                with self.subTest(args=args):
                    code, out, err = self.run_cli(*args)
                    self.assertEqual(code, 2)
                    self.assertEqual(out, "")
                    self.assertIn("error:", err)

    def test_parameters_are_typed_subset_predicates_and_default_groups_keep_all_keys(self):
        for value in (25, "25", True, None):
            self.put(self.records(parameters={"resolution_km": value, "member": "01"}))
        self.put(self.records(parameters={"member": "01"}))
        self.put(self.records(parameters={"resolution_km": 25, "member": "02"}))
        for expression, expected in (
            ("resolution_km=25.0", 2),
            ('resolution_km="25"', 1),
            ("resolution_km=true", 1),
            ("resolution_km=null", 1),
        ):
            _, rows = self.json_cli("--parameter", expression)
            self.assertEqual(rows[-1]["matched"]["visible"], expected)
        _, rows = self.json_cli("--parameter", "resolution_km=25", "--parameter", 'member="01"')
        self.assertEqual(rows[-1]["matched"]["visible"], 1)
        _, rows = self.json_cli("--task", "main.forecast")
        self.assertEqual(len([r for r in rows if r["type"] == "group"]), 6)
        _, rows = self.json_cli("--group-by", "task")
        self.assertEqual(len([r for r in rows if r["type"] == "group"]), 1)

    def test_jsonl_keeps_exact_decimals_large_integers_and_safe_text(self):
        records = self.records(parameters={"threshold": "TOKEN", "label": "汉字\n\x1b[31m"})
        raw = b"".join(encoded(e) + b"\n" for e in records).replace(
            b'"TOKEN"', b"1.0000000000000000001"
        )
        self.put(records, raw=raw)
        code, rows = self.json_cli("--parameter", "threshold=1.0000000000000000001")
        self.assertEqual(code, 0)
        detail = next(r for r in rows if r["type"] == "execution")
        self.assertEqual(
            detail["context"]["parameters"]["threshold"], Decimal("1.0000000000000000001")
        )
        _, rows = self.json_cli("--parameter", "threshold=1.0000000000000000002")
        self.assertTrue(rows[-1]["no_matches"])
        _, text, _ = self.run_cli()
        self.assertIn("\\n\\u001b[31m", text)
        self.assertNotIn("\x1b", text)
        identity = records[0]["execution_id"]
        raw = encoded(self.event(identity, "forecast.progress", {"steps": 9007199254740993}))
        self.assertIn("9007199254740993", cli._json(json.loads(raw)))

    def test_business_event_and_target_cycle_must_match_the_same_event(self):
        records = self.records(cycle=None, kind="multiple")
        identity = records[0]["execution_id"]
        records.extend(
            [
                self.event(identity, "forecast.input", {"target_cycle": CYCLE}),
                self.event(
                    identity, "forecast.output", {"target_cycle": "2024-07-02T00:00:00+08:00"}
                ),
                self.event(identity, "forecast.input", {"target_cycle": "bad"}),
            ]
        )
        self.put(records)
        _, rows = self.json_cli(
            "--target-cycle", "2024-06-30T16:00:00Z", "--event", "forecast.input"
        )
        self.assertEqual(rows[-1]["matched"]["visible"], 1)
        detail = next(r for r in rows if r["type"] == "execution")
        self.assertEqual(detail["context"]["cycle_kind"], "multiple")
        self.assertIsNone(detail["context"]["cycle"])
        self.assertEqual(len(detail["matched_event_ids"]), 1)
        _, rows = self.json_cli("--target-cycle", CYCLE, "--event", "forecast.output")
        self.assertTrue(rows[-1]["no_matches"])
        _, rows = self.json_cli("--cycle", CYCLE)
        self.assertTrue(rows[-1]["no_matches"])
        _, rows = self.json_cli("--cycle-kind", "multiple")
        self.assertEqual(rows[-1]["matched"]["visible"], 1)

    def test_context_conflict_never_passes_final_context_filters_but_sources_are_inspectable(self):
        records = self.records()
        other = self.event(
            records[0]["execution_id"],
            "execution.context",
            {**records[1]["attributes"], "cycle": "2024-07-02T00:00:00+08:00"},
        )
        records.append(other)
        self.put(records)
        identity = records[0]["execution_id"]
        for arguments in (
            ("--cycle", CYCLE),
            ("--task", "main.forecast"),
            ("--parameter", "resolution_km=25"),
        ):
            code, rows = self.json_cli(*arguments)
            self.assertEqual(code, 1)
            self.assertTrue(rows[-1]["no_matches"])
            self.assertEqual(rows[-1]["window_unresolved_context_ids"], [identity])
        code, rows = self.json_cli("--execution-id", identity)
        self.assertEqual(code, 1)
        detail = next(r for r in rows if r["type"] == "execution")
        self.assertEqual(detail["context_status"], "conflicting")
        self.assertIsNone(detail["context"])
        self.assertEqual(len(detail["events"]), 4)
        self.assertTrue(all(e["sources"][0]["line"] for e in detail["events"]))
        self.assertEqual(rows[-1]["uncertain"]["visible"], 1)

    def test_missing_context_end_start_and_stage_end_have_explicit_incompleteness(self):
        records = self.records(started=False, ended=False)
        records.append(
            self.event(
                records[0]["execution_id"],
                "stage.started",
                {"stage": "forecast", "stage_id": str(uuid.uuid4())},
            )
        )
        self.put(records)
        identity = str(uuid.uuid4())
        self.put([self.event(identity, "execution.started", {"task": "entry", "engine": None})])
        code, rows = self.json_cli()
        self.assertEqual(code, 0)
        self.assertEqual(rows[-1]["matched"], {"visible": 2, "started": 1, "missing_start": 1})
        self.assertTrue(rows[-1]["incomplete"])
        self.assertTrue(
            {"missing_start", "context_missing", "end_missing", "incomplete_stage"}
            <= set(rows[-1]["incomplete_reasons"])
        )
        _, text, _ = self.run_cli()
        self.assertIn("end=not_observed", text)
        self.assertNotIn("end=running", text)

    def test_bad_file_and_partial_tail_do_not_hide_good_results_or_filtered_scan_errors(self):
        self.put(self.records())
        broken = self.records(ended=False)
        path = self.put(broken, raw=encoded(broken[0]) + b"\n{bad}\n" + encoded(broken[1]))
        code, rows = self.json_cli()
        self.assertEqual(code, 1)
        self.assertEqual(rows[-1]["matched"]["visible"], 2)
        self.assertEqual(rows[-1]["issue_count"], 2)  # Survey/re-read deduplicated.
        self.assertEqual(
            sorted(r["code"] for r in rows if r["type"] == "issue"),
            ["incomplete_tail", "invalid_json"],
        )
        path.write_bytes(path.read_bytes() + b"\n")
        code, rows = self.json_cli("--task", "no.such.task")
        self.assertEqual(code, 1)
        self.assertTrue(rows[-1]["no_matches"])
        self.assertEqual(rows[-1]["issue_count"], 1)

    def test_rejected_non_utf8_filename_has_valid_utf8_json_error_location(self):
        self.put(self.records())
        parent = self.root / "executions"
        os.mkdir(os.fsencode(parent) + b"/invalid\xff")
        code, rows = self.json_cli()
        self.assertEqual(code, 1)
        location = next(r["source"]["path"] for r in rows if r["type"] == "issue")
        self.assertIn("invalid", location)
        self.assertIn("\\udcff", cli._json(location))
        cli._json(location).encode("utf-8")
        self.assertEqual(rows[-1]["matched"]["started"], 1)

    def test_negative_duration_and_conflicting_end_remain_visible_in_output(self):
        records = self.records()
        records[-1]["occurred_at"] = "2026-10-28T00:29:59.999999999Z"
        records[-1]["attributes"]["duration_ns"] = 7
        self.put(records)
        code, rows = self.json_cli()
        self.assertEqual(code, 0)
        detail = next(r for r in rows if r["type"] == "execution")
        self.assertEqual(detail["duration"]["wall_ns"], -1)
        self.assertEqual(detail["duration"]["declared_ns"], 7)
        self.assertIn("negative_wall_time", rows[-1]["incomplete_reasons"])
        records.append(self.event(records[0]["execution_id"], "execution.ended", {"signal": 15}))
        self.put(records)
        code, rows = self.json_cli()
        self.assertEqual(code, 1)
        detail = next(r for r in rows if r["type"] == "execution")
        self.assertEqual(detail["end_status"], "conflicting")
        self.assertIsNone(detail["result"])
        self.assertIsNone(detail["duration"]["wall_ns"])

    def put_run(self, run_id):
        run = json.loads((CASES / "run-single.json").read_bytes())
        run.update(
            run_id=run_id,
            created_at="2024-07-01T00:00:00+08:00",
            namespace=self.config["namespace"],
        )
        path = (
            self.root / "runs" / partition_date(run["created_at"], self.config) / (run_id + ".json")
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded(run))
        return run

    def test_run_lookup_crosses_creation_dates_and_preserves_missing_references(self):
        identity = str(uuid.uuid4())
        run = self.put_run(identity)
        self.put(self.records(run=identity, scope=run["scope"]))
        self.put(self.records(run=identity, scope=run["scope"], kind="none", cycle=None))
        code, rows = self.json_cli("--run-id", identity)
        self.assertEqual(code, 0)
        self.assertEqual(rows[-1]["matched"]["visible"], 2)
        self.assertTrue(
            next(r for r in rows if r["type"] == "run")["records"][0]["source"]["path"].startswith(
                "runs/20240701/"
            )
        )
        self.assertEqual(
            {r["association"]["status"] for r in rows if r["type"] == "execution"}, {"associated"}
        )
        missing = str(uuid.uuid4())
        self.put(self.records(run=missing, scope="forecast"))
        code, rows = self.json_cli("--run-id", missing)
        self.assertEqual(code, 1)
        self.assertEqual(rows[-1]["matched"]["visible"], 1)
        self.assertEqual(rows[-1]["uncertain"]["visible"], 1)
        self.assertEqual(
            next(r for r in rows if r["type"] == "execution")["association"]["status"], "missing"
        )
        code, rows = self.json_cli(
            "--run-id", identity, "--record-from", "2020-01-01", "--record-through", "2020-01-01"
        )
        self.assertEqual(code, 0)
        self.assertTrue(rows[-1]["no_matches"])
        self.assertEqual(next(r for r in rows if r["type"] == "run")["status"], "found")

    def test_duplicate_execution_files_are_not_matched_as_definite_cycle_or_double_counted(self):
        records = self.records(started=False)
        identity = records[0]["execution_id"]
        self.put(records)
        self.put(records, entry="2026-10-27T08:30:00+08:00")
        code, rows = self.json_cli("--cycle", CYCLE)
        self.assertEqual(code, 1)
        self.assertTrue(rows[-1]["no_matches"])
        self.assertEqual(rows[-1]["duplicate_execution_ids"], [identity])
        code, rows = self.json_cli("--execution-id", identity)
        self.assertEqual(code, 1)
        self.assertEqual(rows[-1]["matched"]["visible"], 1)
        self.assertEqual(rows[-1]["uncertain"]["visible"], 1)
        self.assertEqual(rows[-1]["execution_rows"], 2)

    def test_membership_change_between_scans_is_explicit(self):
        records = self.records()
        self.put(records)
        original = cli.iter_summaries
        calls = 0

        def scans(reader, *window):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.put(self.records())
            yield from original(reader, *window)

        with patch.object(cli, "iter_summaries", scans):
            code, rows = self.json_cli()
        self.assertEqual(code, 1)
        self.assertIn("scan_membership_changed", rows[-1]["incomplete_reasons"])
        self.assertEqual(rows[-1]["scanned"]["visible"], 1)
        self.assertEqual(rows[-1]["matched"]["visible"], 2)

    def test_fatal_root_is_not_created_and_help_has_no_root_dependency(self):
        out, err = io.StringIO(), io.StringIO()
        missing = str(self.root / "not-created")
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = cli.main(["--root", missing, "--format", "jsonl"])
        self.assertEqual(result, 2)
        self.assertEqual(json.loads(err.getvalue())["code"], "root_unavailable")
        self.assertFalse(Path(missing).exists())
        with patch.object(cli, "FileReader", side_effect=AssertionError("root opened")):
            code, out, err = self.run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("--all-record-dates", out)
        self.assertEqual(err, "")

    def test_independent_cli_process_cannot_write_connect_or_import_old_storage(self):
        self.put(self.records())
        before = {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }
        script = r"""
import os,sys
sys.path.insert(0,sys.argv[1])
def audit(event,args):
    if event=="socket.connect" or event.startswith("sqlite3.connect"):
        raise AssertionError(event)
    if event=="open" and args[2] & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND):
        raise AssertionError("query wrote a file")
sys.addaudithook(audit)
from sledra_analysis.execution.cli import main
code=main(["--root",sys.argv[2],"--all-record-dates","--format","jsonl"])
assert code==0
assert not any(n=="sqlite3" or n.startswith(("sledra_analysis.ingest","sledra_analysis.store","sledra_analysis.projections","sledra_analysis.metrics")) for n in sys.modules)
"""
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(ROOT / "analysis/src"), str(self.root)],
            cwd=self.temp.name,
            capture_output=True,
            timeout=15,
            env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        rows = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(rows[-1]["matched"]["visible"], 1)
        self.assertEqual(
            before,
            {
                str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*")
                if p.is_file()
            },
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
