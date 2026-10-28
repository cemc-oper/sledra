"""P3-01: independent Bash comparisons and actual file-query acceptance.

No takflow, ecFlow, synthetic Run, scheduler or business input is used.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "integrations/bash/sledra-execution.sh"
sys.path.insert(0, str(ROOT))
from scripts.test_tools import binary_for, offline_env  # noqa: E402

CYCLE = "2024-07-01T00:00:00+00:00"
TARGET = "2024-06-30T00:00:00+00:00"


class CommonAcceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="sledra p3 common build ")
        cls.addClassCleanup(cls.build.cleanup)
        cls.binary = binary_for("sledra", Path(cls.build.name))

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="sledra p3 common ")
        self.addCleanup(temp.cleanup)
        self.work = Path(temp.name)
        self.env = {
            k: v
            for k, v in offline_env().items()
            if not k.startswith(("SLEDRA_", "EXAMPLE_", "ECF_", "BASH_FUNC_"))
            and k not in {"PYTHONPATH", "BASH_ENV", "ENV"}
        }
        self.env |= {"SLEDRA_BIN": str(self.binary), "SLEDRA_BASH_LIBRARY": str(LIBRARY)}

    def query(self, root, *args, expected=0):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "sledra_analysis.execution.cli",
                "--root",
                str(root),
                "--all-record-dates",
                "--format",
                "jsonl",
                *args,
            ],
            env=self.env | {"PYTHONPATH": str(ROOT / "analysis/src")},
            capture_output=True,
            timeout=20,
        )
        self.assertEqual(result.returncode, expected, result.stderr.decode())
        return [json.loads(line) for line in result.stdout.splitlines()]

    @staticmethod
    def details(rows):
        return [row for row in rows if row["type"] == "execution"]

    def test_three_groups_preserve_business_and_query_real_results(self):
        trial = self.work / "comparison"
        result = subprocess.run(
            ["bash", str(ROOT / "examples/execution/compare.sh"), str(trial)],
            cwd=self.work,
            env=self.env,
            capture_output=True,
            timeout=40,
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode())
        self.assertIn(b"PASS: off/on/missing-recorder", result.stdout)
        rows = self.query(trial / "records")
        details = self.details(rows)
        self.assertEqual(rows[-1]["matched"]["visible"], 4)
        self.assertEqual(len(details), 4)
        self.assertEqual(
            {json.dumps(d["result"], sort_keys=True) for d in details},
            {
                json.dumps(value, sort_keys=True)
                for value in [{"exit_code": 0}, {"exit_code": 7}, {"exit_code": 23}, {"signal": 15}]
            },
        )
        for detail in details:
            self.assertIsNone(detail["entry"]["engine"])
            if detail["result"] == {"exit_code": 23}:
                self.assertEqual(detail["association"]["status"], "context_missing")
                self.assertIsNone(detail["context"])
                self.assertEqual(
                    [e["data"]["name"] for e in detail["events"]],
                    ["execution.started", "execution.ended"],
                )
            else:
                self.assertEqual(detail["association"]["status"], "unlinked")
                self.assertEqual(detail["context"]["cycle_kind"], "none")
                self.assertIsNone(detail["context"]["cycle"])
                self.assertIsNone(detail["context"]["run_id"])
        self.assertIn("window_unresolved_context", rows[-1]["incomplete_reasons"])
        self.assertEqual(len(list((trial / "records").glob("executions/*/*/*.jsonl"))), 4)

    def test_comparison_refuses_to_reuse_output(self):
        (self.work / "marker").write_text("keep")
        result = subprocess.run(
            ["bash", str(ROOT / "examples/execution/compare.sh"), str(self.work)],
            env=self.env,
            capture_output=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((self.work / "marker").read_text(), "keep")
        self.assertFalse((self.work / "records").exists())

    def reader(self, *, kind="single", cycle=CYCLE, root=None, **settings):
        root = root or self.work / "records"
        if not root.exists():
            subprocess.run(
                [
                    str(self.binary),
                    "execution",
                    "init-root",
                    "--root",
                    str(root),
                    "--system",
                    "synthetic",
                    "--deployment",
                    "p3-local",
                    "--partition-timezone=+00:00",
                ],
                env=self.env,
                check=True,
                capture_output=True,
                timeout=10,
            )
        business = self.work / "business"
        business.mkdir(exist_ok=True)
        (business / "input").write_text("prior cycle synthetic bytes\n")
        owner = self.work / "reader.sh"
        owner.write_text(r"""#!/usr/bin/env bash
set -euo pipefail
source "$SLEDRA_BASH_LIBRARY"
sledra_execution_begin --task independent.reader
on_exit() {
    local rc=$?
    trap - EXIT
    sledra_execution_end --exit-code "$rc"
    exit "$rc"
}
trap on_exit EXIT
# Repeat source/begin must keep the same owner and pending context.
source "$SLEDRA_BASH_LIBRARY"
sledra_execution_begin --task independent.reader
args=(--task independent.reader --cycle-kind "$KIND")
if [[ $KIND == single ]]; then
    # This independent application's convention requires a complete timestamp.
    # APP_DATE/APP_HH are deliberately not guessed or defaulted by the library.
    [[ -n $CYCLE ]] || exit 2
    args+=(--cycle "$CYCLE")
fi
sledra_execution_context "${args[@]}" --string member 01
cat input > product
if [[ -n $TARGET_CYCLE ]]; then
    sledra_execution_event input.read.completed --string target_cycle "$TARGET_CYCLE"
fi
# Reading previous-cycle input changes a local variable, not final context.
CYCLE=$TARGET_CYCLE
source "$SLEDRA_BASH_LIBRARY"
""")
        return subprocess.run(
            ["bash", str(owner)],
            cwd=business,
            env=self.env
            | {
                "SLEDRA_MODE": "execution-v2",
                "SLEDRA_ROOT": str(root),
                "KIND": kind,
                "CYCLE": cycle,
                "TARGET_CYCLE": "",
                **settings,
            },
            capture_output=True,
            timeout=15,
        ), root

    def test_repeated_source_restarts_cleanup_and_prior_cycle_input(self):
        first, root = self.reader(TARGET_CYCLE=TARGET)
        self.assertEqual(first.returncode, 0, first.stderr.decode())
        original = {
            p: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.glob("executions/*/*/*.jsonl")
        }
        shutil.rmtree(self.work / "business")
        second, _ = self.reader(root=root, TARGET_CYCLE=TARGET)
        self.assertEqual(second.returncode, 0, second.stderr.decode())
        rows = self.query(root, "--cycle", CYCLE)
        details = self.details(rows)
        self.assertEqual(rows[-1]["matched"]["started"], 2)
        self.assertEqual(len({d["execution_id"] for d in details}), 2)
        for detail in details:
            self.assertEqual(detail["context"]["cycle"], CYCLE)
            self.assertIsNone(detail["entry"]["engine"])
            self.assertIsNone(detail["context"]["run_id"])
            self.assertEqual(
                [e["data"]["name"] for e in detail["events"]],
                [
                    "execution.started",
                    "execution.context",
                    "input.read.completed",
                    "execution.ended",
                ],
            )
        for path, digest in original.items():
            self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)
        target = self.query(root, "--target-cycle", TARGET)
        self.assertEqual(target[-1]["matched"]["visible"], 2)
        self.assertEqual(self.query(root, "--cycle", TARGET)[-1]["matched"]["visible"], 0)
        self.assertEqual(
            (self.work / "business/product").read_text(), "prior cycle synthetic bytes\n"
        )

    def test_explicit_none_multiple_and_missing_single_cycle_do_not_invent_midnight(self):
        for kind in ["none", "multiple"]:
            result, root = self.reader(kind=kind, cycle="", ECF_NAME="/unused/source", HH="00")
            self.assertEqual(result.returncode, 0, result.stderr.decode())
        # Separate date-only and hour-only application configurations both lack
        # the required complete business cycle. No default 00 hour is supplied.
        for values in [{"APP_DATE": "20240701", "APP_HH": ""}, {"APP_DATE": "", "APP_HH": "06"}]:
            result, root = self.reader(cycle="", **values)
            self.assertEqual(result.returncode, 2)
        rows = self.query(root)
        details = self.details(rows)
        self.assertEqual(len(details), 4)
        contexts = [d["context"] for d in details if d["context"] is not None]
        self.assertEqual({c["cycle_kind"] for c in contexts}, {"none", "multiple"})
        self.assertTrue(all(c["cycle"] is None and c["run_id"] is None for c in contexts))
        self.assertEqual(sum(d["context"] is None for d in details), 2)
        self.assertTrue(all(d["entry"]["engine"] is None for d in details))


if __name__ == "__main__":
    unittest.main(verbosity=2)
