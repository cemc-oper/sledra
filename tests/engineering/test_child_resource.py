"""CE controls have an independent embedded schema; legacy resources stay untouched."""

from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.check_contracts import check_child_resource  # noqa: E402
from scripts.sync_contracts import sync_child  # noqa: E402


class ChildResource(unittest.TestCase):
    def test_drift_missing_and_sync_are_scoped_to_child_controls(self):
        with tempfile.TemporaryDirectory(prefix="sledra-child-resource-") as work:
            root = Path(work)
            canonical = root / "spec/execution-child-events/contract.schema.json"
            target = root / "recorder/execution/childcontract/contract.schema.json"
            canonical.parent.mkdir(parents=True)
            target.parent.mkdir(parents=True)
            shutil.copyfile(ROOT / "spec/execution-child-events/contract.schema.json", canonical)
            old = root / "recorder/execution/contract/contract.schema.json"
            old.parent.mkdir(parents=True)
            old.write_bytes(b"unchanged legacy sentinel")
            sync_child(root)
            check_child_resource(root)
            original = canonical.read_bytes()
            target.write_bytes(b"{}")
            with self.assertRaises(ValueError):
                check_child_resource(root)
            self.assertEqual(canonical.read_bytes(), original)
            target.unlink()
            with self.assertRaises(ValueError):
                check_child_resource(root)
            sync_child(root)
            check_child_resource(root)
            self.assertEqual(old.read_bytes(), b"unchanged legacy sentinel")
