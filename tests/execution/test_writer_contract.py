"""Actual Go writer output against the P0 oracle, without Shell or P2 query code."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "spec/execution-v2"))
from check import execution_path, stream_conflicts, validate  # noqa: E402


class WriterContract(unittest.TestCase):
    def test_real_files_are_independent_and_match_reference_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sledra writer contract ") as temp:
            scratch = Path(temp)
            binary = scratch / "test-writer"
            env = os.environ | {"GOPROXY": "off", "GOSUMDB": "off", "GOTOOLCHAIN": "local"}
            subprocess.run(
                ["go", "build", "-o", str(binary), "./execution/testdata/write.go"],
                cwd=ROOT / "recorder",
                env=env,
                capture_output=True,
                check=True,
            )
            root = scratch / "records"
            seen = {}
            config = None
            for _ in range(2):
                result = subprocess.run(
                    [str(binary), str(root)],
                    cwd=scratch,
                    env=env,
                    capture_output=True,
                    check=True,
                )
                self.assertEqual(result.stderr, b"")
                path = result.stdout.decode().strip()
                self.assertNotIn(path, seen)
                raw = (root / path).read_bytes()
                self.assertEqual(stream_conflicts(raw), [])
                events = [validate("event", line) for line in raw.splitlines()]
                self.assertEqual(
                    [e["name"] for e in events],
                    [
                        "execution.started",
                        "execution.context",
                        "example.progress",
                        "execution.ended",
                    ],
                )
                config = validate("root", (root / "sledra.json").read_bytes())
                self.assertEqual(
                    path,
                    execution_path(events[0]["execution_id"], events[0]["occurred_at"], config),
                )
                self.assertIsNone(events[1]["attributes"]["run_id"])
                self.assertEqual(events[2]["attributes"]["steps"], 9007199254740993)
                self.assertEqual(events[2]["attributes"]["message"], "预报\nready")
                seen[path] = raw
            for path, raw in seen.items():
                self.assertEqual((root / path).read_bytes(), raw)
            self.assertEqual(
                {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()},
                set(seen) | {"sledra.json"},
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
