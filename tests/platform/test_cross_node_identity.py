"""Cross-node identity policy for cloned machine IDs on HPC nodes."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cross_node import cross_node_identity  # noqa: E402


class CrossNodeIdentityTests(unittest.TestCase):
    def test_distinct_boots_qualify_even_with_cloned_machine_id(self) -> None:
        a = {"node": "cmac1874", "machine_id": "same", "boot_id_sha256": "boot-a"}
        b = {"node": "cmac1875", "machine_id": "same", "boot_id_sha256": "boot-b"}
        result = cross_node_identity("hpc", a, b)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["identity_basis"], "boot_id_sha256")

    def test_shared_boot_does_not_qualify_distinct_names(self) -> None:
        a = {"node": "a", "machine_id": "machine-a", "boot_id_sha256": "same"}
        b = {"node": "b", "machine_id": "machine-b", "boot_id_sha256": "same"}
        self.assertEqual(cross_node_identity("hpc", a, b)["status"], "unverified")

    def test_legacy_receipts_use_distinct_machine_ids(self) -> None:
        a = {"node": "a", "machine_id": "machine-a"}
        b = {"node": "b", "machine_id": "machine-b"}
        result = cross_node_identity("hpc", a, b)
        self.assertEqual(result["identity_basis"], "machine_id")

    def test_attached_report_remains_unverified(self) -> None:
        a = {"node": "cmac1874", "machine_id": "same"}
        b = {"node": "cmac1875", "machine_id": "same"}
        self.assertEqual(cross_node_identity("hpc", a, b)["status"], "unverified")

    def test_same_host_or_local_mode_is_unverified(self) -> None:
        a = {"node": "a", "boot_id_sha256": "boot-a"}
        b = {"node": "b", "boot_id_sha256": "boot-b"}
        self.assertEqual(cross_node_identity("local", a, b)["status"], "unverified")
        b["node"] = "a"
        self.assertEqual(cross_node_identity("hpc", a, b)["status"], "unverified")


if __name__ == "__main__":
    unittest.main()
