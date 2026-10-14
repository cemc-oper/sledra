"""Real-process regression for isolation, foreign binaries and lost workers."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.test_tools import binary_for  # noqa: E402
from scripts.test_session import TestSession  # noqa: E402


class HarnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = tempfile.TemporaryDirectory(prefix="sledra-harness-tests-")
        cls.binary = binary_for("sledra", Path(cls.scratch.name))

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scratch.cleanup()

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(prefix="case-", dir=self.scratch.name)
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("SLEDRA_", "STANDALONE_"))
        }
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"

    def command(self, script: str, *args: str) -> list[str]:
        return [sys.executable, str(ROOT / script), *args]

    def read(self, path: Path) -> dict:
        return json.loads(path.read_text())

    def test_parallel_runs_use_only_selected_parents(self) -> None:
        children = []
        for index in range(2):
            parent = self.root / f"parent {index}"
            parent.mkdir()
            (parent / "sentinel").write_text("preserve")
            report = self.root / f"report-{index}"
            args = [
                "simulate",
                "--bin",
                str(self.binary),
                "--work-root",
                str(parent),
                "--report-dir",
                str(report),
                "--samples",
                "2",
                "--timeout",
                "20",
            ] + (["--keep-work"] if index else [])
            child = subprocess.Popen(
                self.command("tests/platform/cross_node.py", *args),
                env=self.env,
                cwd=self.root,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            children.append((child, parent, report, bool(index)))
        for child, parent, report, keep in children:
            out, err = child.communicate(timeout=30)
            self.assertEqual(child.returncode, 0, (out, err))
            summary = self.read(report / "summary.json")
            self.assertEqual(summary["status"], "pass")
            work = Path(summary["work_directory"])
            self.assertEqual(work.parent, parent)
            self.assertEqual(work.exists(), keep)
            self.assertEqual((parent / "sentinel").read_text(), "preserve")
            collection = self.read(report / "collection/summary.json")
            self.assertEqual(collection["real_cross_node"]["status"], "unverified")
            self.assertEqual(collection["registry_records"], 2)
            self.assertEqual(collection["process_events"], 2)
            self.assertEqual(
                collection["fixed_context"]["path"],
                str(work / "observability/contexts/a-attempt.json"),
            )

    def test_lost_worker_nonzero_with_partial_report_and_retained_work(self) -> None:
        report = self.root / "lost"
        result = subprocess.run(
            self.command(
                "tests/platform/cross_node.py",
                "simulate",
                "--bin",
                str(self.binary),
                "--work-root",
                str(self.root),
                "--report-dir",
                str(report),
                "--omit-role",
                "b",
                "--timeout",
                "1",
                "--samples",
                "1",
            ),
            env=self.env,
            capture_output=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        summary = self.read(report / "summary.json")
        self.assertEqual(summary["status"], "fail")
        self.assertTrue(summary["work_retained"])
        self.assertTrue(Path(summary["work_directory"]).is_dir())
        collection = self.read(report / "collection/summary.json")
        self.assertIn("missing worker", collection["reason"])
        self.assertIn("timed out", collection["roles"]["a"]["reason"])

    def test_preflight_failures_are_reported_before_foreign_execution(self) -> None:
        foreign = self.root / "foreign"
        content = bytearray(self.binary.read_bytes())
        content[18:20] = (183 if content[18:20] == (62).to_bytes(2, "little") else 62).to_bytes(
            2, "little"
        )
        foreign.write_bytes(content)
        foreign.chmod(0o700)
        bad_spec = self.root / "empty-spec"
        bad_spec.mkdir()
        for name, args, environment, reason in (
            (
                "hpc-root",
                ["--mode", "hpc", "--bin", str(self.binary)],
                self.env,
                "explicit --work-root",
            ),
            ("missing-bin", ["--bin", str(self.root / "missing")], self.env, "No such file"),
            ("foreign", ["--bin", str(foreign)], self.env, "architecture"),
            ("missing-bash", ["--bin", str(self.binary)], dict(self.env, PATH=""), "bash"),
            (
                "missing-spec",
                ["--bin", str(self.binary), "--spec-dir", str(bad_spec)],
                self.env,
                "contract_unavailable",
            ),
        ):
            with self.subTest(name=name):
                if name != "hpc-root":
                    args = [*args, "--work-root", str(self.root)]
                report = self.root / f"report-{name}"
                result = subprocess.run(
                    self.command("tests/m1/verify.py", *args, "--report-dir", str(report)),
                    env=environment,
                    capture_output=True,
                    timeout=10,
                )
                self.assertNotEqual(result.returncode, 0)
                summary = self.read(report / "summary.json")
                self.assertEqual(summary["status"], "fail")
                self.assertIn(reason, summary["reason"])
                if name == "foreign":
                    self.assertFalse(
                        any(
                            c["argv"][0] == str(foreign)
                            for c in self.read(report / "commands.json")
                        )
                    )

    def test_cleanup_refuses_changed_owner_and_reports_failure(self) -> None:
        args = argparse.Namespace(
            bin=self.binary,
            spec_dir=None,
            work_root=self.root,
            report_dir=self.root / "owner-report",
            keep_work=False,
            mode="local",
        )
        session = TestSession(args, "owner-test")
        with self.assertRaisesRegex(ValueError, "owner token"):
            with session:
                (session.work / ".sledra-test-run.json").write_text('{"token":"other-run"}')
        self.assertTrue(session.work.is_dir())
        self.assertEqual(self.read(session.report / "summary.json")["status"], "fail")

    def test_local_session_without_report_cleans_owned_work(self) -> None:
        args = argparse.Namespace(
            bin=self.binary,
            spec_dir=None,
            work_root=self.root,
            report_dir=None,
            keep_work=False,
            mode="local",
        )
        with TestSession(args, "no-report") as session:
            self.assertIsNone(session.report)
            work = session.work
            self.assertTrue(work.is_dir())
        self.assertFalse(work.exists())
        self.assertEqual(list(self.root.iterdir()), [])

    def test_collection_failure_has_report(self) -> None:
        (self.root / "coord").mkdir()
        (self.root / "coord/result-a.json").write_text("{invalid-json")
        report = self.root / "collection"
        result = subprocess.run(
            self.command(
                "tests/platform/cross_node.py",
                "collect",
                "--run-dir",
                str(self.root),
                "--report-dir",
                str(report),
                "--timeout",
                "0.1",
            ),
            env=self.env,
            capture_output=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.read(report / "summary.json")["status"], "fail")


if __name__ == "__main__":
    unittest.main(verbosity=2)
