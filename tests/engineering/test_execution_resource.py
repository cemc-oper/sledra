"""Execution resource drift fails without modifying canonical or embedded bytes."""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.check_contracts import check_execution_resource  # noqa: E402


class ExecutionResource(unittest.TestCase):
    def test_resource_match_drift_and_missing_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sledra-execution-resource-") as temp:
            root = Path(temp)
            source = root / "spec/execution-v2/contract.schema.json"
            target = root / "recorder/execution/contract/contract.schema.json"
            for path in (source, target):
                path.parent.mkdir(parents=True)
                shutil.copyfile(ROOT / "spec/execution-v2/contract.schema.json", path)
            check_execution_resource(root)
            canonical = source.read_bytes()
            target.write_bytes(b"{}\n")
            with self.assertRaisesRegex(ValueError, "differs from canonical"):
                check_execution_resource(root)
            self.assertEqual(source.read_bytes(), canonical)
            self.assertEqual(target.read_bytes(), b"{}\n")
            target.unlink()
            with self.assertRaisesRegex(ValueError, "differs from canonical"):
                check_execution_resource(root)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
