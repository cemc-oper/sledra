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
from scripts.sync_contracts import sync_execution  # noqa: E402


class ExecutionResource(unittest.TestCase):
    def test_python_resource_match_drift_and_missing_are_read_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sledra-python-resource-") as temp:
            root = Path(temp)
            (root / "analysis/src/sledra_analysis/execution").mkdir(parents=True)
            source = root / "spec/execution-v2/contract.schema.json"
            go = root / "recorder/execution/contract/contract.schema.json"
            target = (
                root / "analysis/src/sledra_analysis/resources/execution_v2/contract.schema.json"
            )
            for path in (source, go, target):
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / "spec/execution-v2/contract.schema.json", path)
            check_execution_resource(root)
            canonical = source.read_bytes()
            target.write_bytes(b"{}\n")
            with self.assertRaisesRegex(ValueError, "execution_v2/contract.schema.json"):
                check_execution_resource(root)
            self.assertEqual(source.read_bytes(), canonical)
            self.assertEqual(go.read_bytes(), canonical)
            self.assertEqual(target.read_bytes(), b"{}\n")
            target.unlink()
            with self.assertRaisesRegex(ValueError, "execution_v2/contract.schema.json"):
                check_execution_resource(root)
            self.assertFalse(target.exists())

    def test_v2_sync_restores_only_the_new_resource_targets(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sledra-v2-sync-") as temp:
            root = Path(temp)
            for component in ("recorder/execution", "analysis/src/sledra_analysis/execution"):
                (root / component).mkdir(parents=True)
            source = root / "spec/execution-v2/contract.schema.json"
            source.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / "spec/execution-v2/contract.schema.json", source)
            legacy = root / "analysis/src/sledra_analysis/resources/contract/keep.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_bytes(b"frozen legacy sentinel")
            before = {
                str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
            }
            sync_execution(root)
            check_execution_resource(root)
            for name, raw in before.items():
                self.assertEqual((root / name).read_bytes(), raw)
            after = {str(p.relative_to(root)) for p in root.rglob("*") if p.is_file()}
            self.assertEqual(
                after - set(before),
                {
                    "recorder/execution/contract/contract.schema.json",
                    "analysis/src/sledra_analysis/resources/execution_v2/contract.schema.json",
                },
            )

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
