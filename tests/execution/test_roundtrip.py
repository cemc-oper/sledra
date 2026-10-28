"""P2 closure: real independent Go/Bash records -> installed file query.

Run/alias/parameter fixtures are explicitly synthetic and tested separately.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.test_tools import binary_for, offline_env  # noqa: E402

CYCLE = "2024-07-01T00:00:00+08:00"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


class InstalledRoundtrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="sledra roundtrip installed ")
        cls.addClassCleanup(cls.build.cleanup)
        cls.install = Path(cls.build.name)
        cls.env = {
            k: v
            for k, v in offline_env().items()
            if not k.startswith(("SLEDRA_", "EXAMPLE_", "BUSINESS_", "BASH_FUNC_"))
            and k not in {"PYTHONPATH", "BASH_ENV", "ENV"}
        }
        cls.binary = cls.install / "sledra"
        shutil.copyfile(binary_for("sledra", cls.install / "build"), cls.binary)
        cls.binary.chmod(0o555)
        for name, original in (
            ("sledra-execution.sh", ROOT / "integrations/bash/sledra-execution.sh"),
            ("standalone.sh", ROOT / "examples/execution/standalone.sh"),
        ):
            shutil.copyfile(original, cls.install / name)
            (cls.install / name).chmod(0o444)
        cls.producer = cls.install / "go-producer"
        subprocess.run(
            ["go", "build", "-o", str(cls.producer), "./execution/testdata/write.go"],
            cwd=ROOT / "recorder",
            env=cls.env,
            capture_output=True,
            check=True,
            timeout=60,
        )
        subprocess.run(
            [
                "uv",
                "build",
                "--offline",
                "--no-python-downloads",
                "--out-dir",
                str(cls.install / "dist"),
            ],
            cwd=ROOT / "analysis",
            env=cls.env,
            capture_output=True,
            check=True,
            timeout=60,
        )
        wheel = next((cls.install / "dist").glob("*.whl"))
        cls.query_install = cls.install / "query"
        result = subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--offline",
                "--no-python-downloads",
                "--no-deps",
                "--python",
                sys.executable,
                "--target",
                str(cls.query_install),
                str(wheel),
            ],
            cwd=cls.install,
            env=cls.env,
            capture_output=True,
            timeout=60,
        )
        if result.returncode:
            raise AssertionError(result.stderr)
        cls.entry = cls.query_install / "bin/sledra-history"
        assert cls.entry.is_file()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sledra roundtrip work ")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.root = self.work / "records"
        self.business = self.work / "business.sh"
        self.business.write_text("""#!/bin/bash
set -euo pipefail
printf 'run\\n' >> "$BUSINESS_LOG"
printf '%s\\0' "$@" >> "$BUSINESS_ARGV"
printf 'original stdout\\n'
printf 'original stderr\\n' >&2
exit "${BUSINESS_RC:-0}"
""")
        self.business.chmod(0o700)

    def initialize(self, root=None):
        subprocess.run(
            [
                str(self.binary),
                "execution",
                "init-root",
                "--root",
                str(root or self.root),
                "--system",
                "synthetic",
                "--deployment",
                "roundtrip",
                "--partition-timezone=+08:00",
            ],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            check=True,
            timeout=10,
        )

    def snapshot(self, root=None):
        root = root or self.root
        return {
            str(p.relative_to(root)): (p.read_bytes(), p.stat().st_mtime_ns)
            for p in root.rglob("*")
            if p.is_file()
        }

    def invoke(self, *, rc=0, configure_rc=None, callback_zero=False, mode="execution-v2"):
        case = Path(tempfile.mkdtemp(prefix="business-", dir=self.work))
        env = self.env | {
            "SLEDRA_BIN": str(self.binary),
            "SLEDRA_ROOT": str(self.root),
            "SLEDRA_MODE": mode,
            "SLEDRA_BASH_LIBRARY": str(self.install / "sledra-execution.sh"),
            "EXAMPLE_CYCLE_KIND": "single",
            "EXAMPLE_CYCLE": CYCLE,
            "EXAMPLE_CALLBACK_LOG": str(case / "callback"),
            "EXAMPLE_CALLBACK_EXIT_ZERO": "1" if callback_zero else "0",
            "BUSINESS_LOG": str(case / "calls"),
            "BUSINESS_ARGV": str(case / "argv"),
            "BUSINESS_RC": str(rc),
        }
        if configure_rc is not None:
            configure = case / "configure.sh"
            configure.write_text(f"#!/bin/bash\nexit {configure_rc}\n")
            configure.chmod(0o700)
            env["EXAMPLE_CONFIGURE"] = str(configure)
        result = subprocess.run(
            [
                "bash",
                str(self.install / "standalone.sh"),
                str(self.business),
                "argument with spaces",
                "$(touch should-not-exist)",
            ],
            cwd=case,
            env=env,
            capture_output=True,
            timeout=15,
        )
        return result, case

    def query(self, *args, root=None, expected=0):
        root = root or self.root
        before = self.snapshot(root)
        # Execute the actual installed console script with runtime dependencies,
        # isolated from source paths; prohibit query writes and legacy services.
        script = r"""
import os,runpy,sys
sys.path.insert(0,sys.argv[1])
installed=sys.argv[1]
def audit(event,args):
    if event=="socket.connect" or event.startswith("sqlite3.connect"):
        raise AssertionError(event)
    if event=="open" and args[2] & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND):
        raise AssertionError("query attempted a write")
sys.addaudithook(audit)
sys.argv=[sys.argv[2],*sys.argv[3:]]
try:
    runpy.run_path(sys.argv[0],run_name="__main__")
except SystemExit as exc:
    code=exc.code
else:
    raise AssertionError("console did not exit")
import sledra_analysis.execution.cli as cli
assert cli.__file__.startswith(installed)
assert not any(n=="sqlite3" or n.startswith(("sledra_analysis.ingest","sledra_analysis.store","sledra_analysis.projections","sledra_analysis.metrics")) for n in sys.modules)
raise SystemExit(code)
"""
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                "-c",
                script,
                str(self.query_install),
                str(self.entry),
                "--root",
                str(root),
                "--format",
                "jsonl",
                *args,
            ],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, expected, result.stderr or result.stdout[-3000:])
        self.assertEqual(result.stderr, b"")
        self.assertEqual(before, self.snapshot(root))
        rows = [json.loads(line, parse_float=Decimal) for line in result.stdout.splitlines()]
        self.assertEqual(rows[0]["type"], "coverage")
        self.assertEqual(rows[-1]["type"], "summary")
        return rows, result.stdout

    def details(self, rows):
        return [r for r in rows if r["type"] == "execution"]

    def test_real_go_writer_two_executions_are_read_from_installed_cli(self):
        files = []
        for _ in range(2):
            result = subprocess.run(
                [str(self.producer), str(self.root)],
                cwd=self.work,
                env=self.env,
                capture_output=True,
                check=True,
                timeout=10,
            )
            files.append(result.stdout.decode().strip())
        self.assertNotEqual(files[0], files[1])
        rows, _ = self.query("--all-record-dates", "--task", "example.check")
        self.assertEqual(rows[-1]["matched"], {"visible": 2, "started": 2, "missing_start": 0})
        for detail in self.details(rows):
            self.assertEqual(detail["association"]["status"], "unlinked")
            self.assertEqual(detail["context"]["cycle_kind"], "none")
            progress = next(
                e["data"]["attributes"]
                for e in detail["events"]
                if e["data"]["name"] == "example.progress"
            )
            self.assertEqual(progress["steps"], 9007199254740993)
            self.assertEqual(progress["fraction"], Decimal("0.125"))
            self.assertEqual(progress["message"], "预报\nready")
            self.assertEqual(detail["result"], {"exit_code": 0})
        self.assertEqual(set(self.snapshot()), {"sledra.json", *files})

    def test_real_bash_business_results_restarts_and_output_deletion_rebuild(self):
        self.initialize()
        baseline, baseline_case = self.invoke(mode="off", rc=5, callback_zero=True)
        failure, failure_case = self.invoke(rc=5, callback_zero=True)
        success, success_case = self.invoke()
        retry, retry_case = self.invoke()
        config, config_case = self.invoke(configure_rc=7)
        self.assertEqual(
            (
                baseline.returncode,
                failure.returncode,
                success.returncode,
                retry.returncode,
                config.returncode,
            ),
            (0, 0, 0, 0, 7),
        )
        self.assertEqual((baseline.stdout, baseline.stderr), (failure.stdout, failure.stderr))
        for case in (baseline_case, failure_case, success_case, retry_case):
            self.assertEqual((case / "calls").read_bytes(), b"run\n")
            self.assertEqual(
                (case / "argv").read_bytes(), b"argument with spaces\0$(touch should-not-exist)\0"
            )
            self.assertFalse((case / "should-not-exist").exists())
        self.assertFalse((config_case / "calls").exists())
        self.assertEqual((failure_case / "callback").read_bytes(), b"exit:5\n")
        rows, raw = self.query("--all-record-dates")
        self.assertEqual(rows[-1]["matched"], {"visible": 4, "started": 4, "missing_start": 0})
        details = self.details(rows)
        self.assertEqual(len({d["execution_id"] for d in details}), 4)
        self.assertEqual(sorted(d["result"]["exit_code"] for d in details), [0, 0, 5, 7])
        self.assertEqual(sum(d["context_status"] == "missing" for d in details), 1)
        self.assertTrue(
            all(d["association"]["status"] in ("unlinked", "context_missing") for d in details)
        )
        # Save/delete only this test's query output, then rebuild solely from root.
        output = self.work / "query.jsonl"
        output.write_bytes(raw)
        output.unlink()
        rebuilt, raw_again = self.query("--all-record-dates")
        self.assertEqual(raw_again, raw)
        self.assertEqual(rebuilt[-1]["matched"], rows[-1]["matched"])
        selected, _ = self.query(
            "--all-record-dates", "--cycle", CYCLE, "--parameter", "resolution_km=25"
        )
        self.assertEqual(selected[-1]["matched"]["started"], 3)
        self.assertTrue(selected[-1]["incomplete"])
        self.assertIn("window_unresolved_context", selected[-1]["incomplete_reasons"])
        # An old business cycle lives in the actual current entry partition.
        one = self.details(selected)[0]
        partition = one["events"][0]["sources"][0]["path"].split("/")[1]
        date = f"{partition[:4]}-{partition[4:6]}-{partition[6:]}"
        today, _ = self.query("--record-from", date, "--record-through", date, "--cycle", CYCLE)
        self.assertGreaterEqual(today[-1]["matched"]["visible"], 1)
        empty, _ = self.query(
            "--record-from",
            "2000-01-01",
            "--record-through",
            "2000-01-01",
            "--execution-id",
            one["execution_id"],
        )
        self.assertTrue(empty[-1]["no_matches"])
        self.assertEqual(empty[0]["record_from"], "2000-01-01")
        self.assertEqual(len(self.snapshot()), 5)  # Only root config + four files.

    def test_real_bash_multiple_cycles_exact_numbers_and_two_same_name_stages(self):
        self.initialize()
        script = r"""
set -euo pipefail
source "$SLEDRA_BASH_LIBRARY"
sledra_execution_begin --task actual.batch
sledra_execution_context --task actual.batch --cycle-kind multiple --int resolution_km 25 --float threshold 1.0000000000000000001
for step in 1 2; do
    sledra_stage_begin forecast
    sledra_execution_event forecast.input --string target_cycle 2024-07-01T00:00:00+08:00 --int steps 9007199254740993 --float threshold 1.0000000000000000001 --string message $'预报\nready'
    sledra_stage_end --exit-code 0
done
sledra_execution_end --exit-code 0
"""
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=self.work,
            env=self.env
            | {
                "SLEDRA_MODE": "execution-v2",
                "SLEDRA_ROOT": str(self.root),
                "SLEDRA_BIN": str(self.binary),
                "SLEDRA_BASH_LIBRARY": str(self.install / "sledra-execution.sh"),
            },
            capture_output=True,
            timeout=15,
        )
        self.assertEqual((result.returncode, result.stderr), (0, b""))
        rows, _ = self.query(
            "--all-record-dates", "--target-cycle", CYCLE, "--event", "forecast.input"
        )
        detail = self.details(rows)[0]
        self.assertEqual(detail["context"]["cycle_kind"], "multiple")
        self.assertIsNone(detail["context"]["cycle"])
        self.assertEqual(len(detail["stages"]), 2)
        self.assertEqual(len({s["stage_id"] for s in detail["stages"]}), 2)
        self.assertEqual([s["name"] for s in detail["stages"]], ["forecast", "forecast"])
        for e in detail["events"]:
            if e["data"]["name"] == "forecast.input":
                self.assertEqual(
                    e["data"]["attributes"]["threshold"], Decimal("1.0000000000000000001")
                )
                self.assertEqual(e["data"]["attributes"]["steps"], 9007199254740993)
                self.assertEqual(e["data"]["attributes"]["message"], "预报\nready")
        self.assertEqual(rows[-1]["matched"]["visible"], 1)
        empty, _ = self.query("--all-record-dates", "--cycle", CYCLE)
        self.assertTrue(empty[-1]["no_matches"])

    def test_real_owner_kill_leaves_missing_end_and_stage_endpoint_unknown(self):
        self.initialize()
        script = r"""
source "$SLEDRA_BASH_LIBRARY"
sledra_execution_begin --task actual.killed
sledra_execution_context --task actual.killed --cycle-kind none
sledra_stage_begin work
printf 'business\n' > "$BUSINESS_LOG"
kill -KILL "$BASHPID"
"""
        result = subprocess.run(
            ["bash", "-c", script],
            cwd=self.work,
            env=self.env
            | {
                "SLEDRA_MODE": "execution-v2",
                "SLEDRA_ROOT": str(self.root),
                "SLEDRA_BIN": str(self.binary),
                "SLEDRA_BASH_LIBRARY": str(self.install / "sledra-execution.sh"),
                "BUSINESS_LOG": str(self.work / "started"),
            },
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, -9)
        self.assertEqual((self.work / "started").read_bytes(), b"business\n")
        rows, _ = self.query("--all-record-dates")
        detail = self.details(rows)[0]
        self.assertEqual(detail["end_status"], "missing")
        self.assertIsNone(detail["result"])
        self.assertIsNone(detail["duration"]["wall_ns"])
        self.assertEqual(detail["stages"][0]["end_status"], "missing")
        self.assertTrue(rows[-1]["incomplete"])
        self.assertNotIn("running", {detail["end_status"], detail["start_status"]})

    def test_fault_injection_on_real_files_preserves_other_executions_and_sources(self):
        self.initialize()
        self.invoke()
        self.invoke()
        files = sorted(self.root.glob("executions/*/*/*.jsonl"))
        path = files[0]
        original = path.read_bytes()
        records = [json.loads(line) for line in original.splitlines()]
        progress = next(e for e in records if e["name"] == "example.progress")
        line = encoded(progress) + b"\n"
        path.write_bytes(original + line + line[:20])
        rows, _ = self.query("--all-record-dates", expected=1)
        self.assertEqual(rows[-1]["matched"]["visible"], 2)
        self.assertEqual([r["code"] for r in rows if r["type"] == "issue"], ["incomplete_tail"])
        path.write_bytes(path.read_bytes() + line[20:])
        rows, _ = self.query("--all-record-dates")
        detail = next(
            d for d in self.details(rows) if d["execution_id"] == progress["execution_id"]
        )
        observed = next(
            e for e in detail["events"] if e["data"]["event_id"] == progress["event_id"]
        )
        self.assertEqual(len(observed["sources"]), 3)
        changed_event = {**progress, "attributes": {"fraction": 0.5}}
        changed_context = {
            **next(e for e in records if e["name"] == "execution.context"),
            "event_id": str(uuid.uuid4()),
        }
        changed_context["attributes"] = {
            **changed_context["attributes"],
            "cycle": "1999-01-01T00:00:00Z",
        }
        changed_end = {
            **next(e for e in records if e["name"] == "execution.ended"),
            "event_id": str(uuid.uuid4()),
            "attributes": {"signal": 15},
        }
        with path.open("ab") as stream:
            stream.write(b"{bad}\n" + b"x" * 65537 + b"\n")
            for record in (changed_event, changed_context, changed_end):
                stream.write(encoded(record) + b"\n")
        rows, _ = self.query("--all-record-dates", expected=1)
        self.assertEqual(rows[-1]["matched"]["visible"], 2)
        self.assertEqual(rows[-1]["uncertain"]["visible"], 1)
        detail = next(
            d for d in self.details(rows) if d["execution_id"] == progress["execution_id"]
        )
        self.assertTrue(
            {"conflicting_event_id", "conflicting_context", "conflicting_end"}
            <= set(detail["conflicts"])
        )
        self.assertIsNone(detail["context"])
        self.assertIsNone(detail["result"])
        self.assertEqual(
            sorted(r["code"] for r in rows if r["type"] == "issue"),
            ["invalid_json", "limit_exceeded"],
        )
        filtered, _ = self.query("--all-record-dates", "--cycle", CYCLE, expected=1)
        self.assertEqual(filtered[-1]["matched"]["visible"], 1)
        self.assertTrue(all(e["sources"] for d in self.details(rows) for e in d["events"]))

    def test_synthetic_run_alias_resolutions_and_auxiliary_are_separate_from_real_pipeline(self):
        # Only this test writes synthetic Run/context/engine facts. No runtime
        # Run creation/propagation/acceptance service or ecFlow source is used.
        root = self.work / "synthetic-records"
        self.initialize(root)
        run_ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        for identity in run_ids:
            run = {
                "format_version": "2.0.0",
                "run_id": identity,
                "namespace": {"system": "synthetic", "deployment": "roundtrip"},
                "scope": "forecast",
                "created_at": CYCLE,
                "cycle_kind": "single",
                "cycle": CYCLE,
                "creator_execution_id": str(uuid.uuid4()),
            }
            path = root / "runs/20240701" / (identity + ".json")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(encoded(run))
        cases = [
            (run_ids[0], 3, "single", CYCLE, False, "forecast"),
            (run_ids[0], 25, "single", CYCLE, False, "forecast"),
            (run_ids[1], 25, "single", CYCLE, False, "forecast"),
            (None, 25, "single", "2024-06-30T00:00:00+08:00", True, "forecast"),
            (run_ids[0], 25, "none", None, False, "cleanup"),
        ]
        alias_id = None
        for run_id, resolution, kind, cycle, alias, task in cases:
            identity = str(uuid.uuid4())
            if alias:
                alias_id = identity
            records = []
            for name, attrs in (
                (
                    "execution.started",
                    {
                        "task": task,
                        "engine": {
                            "kind": "ecflow",
                            "service": "synthetic",
                            "node": "/synthetic/alias0" if alias else "/synthetic/task",
                            "alias": alias,
                        },
                    },
                ),
                (
                    "execution.context",
                    {
                        "task": task,
                        "parameters": {"resolution_km": resolution},
                        "scope": "forecast",
                        "cycle_kind": kind,
                        "cycle": cycle,
                        "run_id": run_id,
                    },
                ),
                ("execution.ended", {"exit_code": 0}),
            ):
                records.append(
                    {
                        "format_version": "2.0.0",
                        "event_id": str(uuid.uuid4()),
                        "execution_id": identity,
                        "name": name,
                        "occurred_at": "2026-10-28T08:00:00+08:00",
                        "attributes": attrs,
                    }
                )
            bucket = hashlib.sha256(identity.encode()).hexdigest()[0]
            path = root / "executions/20261028" / bucket / (identity + ".jsonl")
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"".join(encoded(e) + b"\n" for e in records))
        rows, _ = self.query("--all-record-dates", root=root)
        self.assertEqual(rows[-1]["matched"]["visible"], 5)
        self.assertEqual(len([r for r in rows if r["type"] == "group"]), 5)
        by_run, _ = self.query("--all-record-dates", "--run-id", run_ids[0], root=root)
        self.assertEqual(by_run[-1]["matched"]["visible"], 3)
        auxiliary = next(d for d in self.details(by_run) if d["context"]["task"] == "cleanup")
        self.assertIsNone(auxiliary["context"]["cycle"])
        self.assertEqual(auxiliary["association"]["status"], "associated")
        by_cycle, _ = self.query("--all-record-dates", "--cycle", CYCLE, root=root)
        self.assertEqual(by_cycle[-1]["matched"]["visible"], 3)
        alias, _ = self.query("--all-record-dates", "--execution-id", alias_id, root=root)
        self.assertEqual(self.details(alias)[0]["association"]["status"], "unlinked")
        selected, _ = self.query("--all-record-dates", "--parameter", "resolution_km=3", root=root)
        self.assertEqual(selected[-1]["matched"]["visible"], 1)
        # A synthetic creation without an Execution must remain a visible round.
        unused = json.loads((root / "runs/20240701" / (run_ids[0] + ".json")).read_bytes())
        unused["run_id"] = str(uuid.uuid4())
        unused["creator_execution_id"] = str(uuid.uuid4())
        (root / "runs/20240701" / (unused["run_id"] + ".json")).write_bytes(encoded(unused))
        rounds, _ = self.query(
            "--all-record-dates", "--all-run-records", "--cycle", CYCLE, root=root
        )
        self.assertEqual(rounds[-1]["run_creations"]["recorded_creations"], 3)
        self.assertEqual(rounds[-1]["run_creations"]["with_followup_observed"], 2)
        self.assertEqual(rounds[-1]["run_creations"]["without_followup_observed"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
