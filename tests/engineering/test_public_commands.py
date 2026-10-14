"""Public command failures stay visible and never create automatic report trees."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROTECTED = (
    "spec/1.0.0",
    "spec/fixtures",
    "recorder/event/contract",
    "analysis/src/sledra_analysis/resources/contract",
    "recorder/internal/speccontract/contract",
    "examples/standalone/fixtures",
    "tests/m1/results",
    "tests/platform/results",
)


class PublicCommands(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sledra commands with spaces ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name in (
            "Makefile",
            "recorder/Makefile",
            "docs/Makefile",
            "scripts/guard_protected.sh",
        ):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        for name in PROTECTED:
            (self.root / name).mkdir(parents=True, exist_ok=True)
            (self.root / name / "frozen").write_bytes(b"protected input\n")
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for name in ("uv", "go"):
            tool = self.tools / name
            tool.write_text(f"#!/bin/sh\necho 'injected {name} failure' >&2\nexit 37\n")
            tool.chmod(0o755)

    def run_make(self, target: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["make", "UV=uv", target],
            cwd=self.root,
            env=os.environ | {"PATH": f"{self.tools}:{os.environ['PATH']}"},
            capture_output=True,
            text=True,
            timeout=30,
        )

    def assert_no_reports(self) -> None:
        self.assertFalse((self.root / "test-results").exists())
        self.assertFalse(list(self.root.rglob("summary.json")))
        self.assertFalse(list(self.root.rglob("*.log")))
        for name in PROTECTED:
            self.assertEqual((self.root / name / "frozen").read_bytes(), b"protected input\n")

    def test_help_succeeds_without_building_or_reporting(self) -> None:
        result = self.run_make("help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("make benchmark", result.stdout)
        self.assertFalse((self.root / "build").exists())
        self.assert_no_reports()

    def test_component_failures_reach_public_console_without_reports(self) -> None:
        for target, diagnostic in (("build", "go"), ("check", "uv"), ("docs", "uv")):
            with self.subTest(target=target):
                result = self.run_make(target)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"injected {diagnostic} failure", result.stderr)
                self.assert_no_reports()

    def test_protection_rejects_new_fixture_and_preserves_command_failure(self) -> None:
        guard = ["bash", "scripts/guard_protected.sh"]
        failed = subprocess.run([*guard, "sh", "-c", "exit 37"], cwd=self.root)
        self.assertEqual(failed.returncode, 37)
        changed = subprocess.run(
            [*guard, "sh", "-c", "printf injected > spec/fixtures/new-file"],
            cwd=self.root,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(changed.returncode, 0)
        self.assertIn("Protected fixture bytes changed", changed.stderr)
        self.assertIn("spec/fixtures/new-file", changed.stderr)
        self.assert_no_reports()


if __name__ == "__main__":
    unittest.main(verbosity=2)
