"""Workflow errors and missing/tampered validator caches must fail offline."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.check_ci import check, checked_binary, recipe, sha, setup  # noqa: E402


class CIChecks(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sledra-ci-check-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / ".github").mkdir()
        shutil.copyfile(ROOT / ".github/ci-tools.json", self.root / ".github/ci-tools.json")

    def test_missing_validator_fails_without_network(self) -> None:
        with patch("scripts.check_ci.urllib.request.urlopen") as network:
            with self.assertRaisesRegex(ValueError, "missing actionlint"):
                check(self.root)
            network.assert_not_called()

    def test_tampered_cache_is_rejected_before_execution(self) -> None:
        binary, _ = checked_binary(ROOT)
        directory = self.root / "build/ci-tools" / recipe(self.root)["target"]
        directory.mkdir(parents=True)
        destination = directory / "actionlint"
        shutil.copyfile(binary, destination)
        destination.chmod(0o700)
        shutil.copyfile(binary.parent / "LICENSE.txt", directory / "LICENSE.txt")
        (directory / "manifest.json").write_text(
            json.dumps(
                {
                    "recipe": recipe(self.root),
                    "binary_sha256": sha(destination),
                    "license_sha256": sha(directory / "LICENSE.txt"),
                }
            )
        )
        with destination.open("ab") as output:
            output.write(b"corrupted-cache")
        with patch("scripts.check_ci.subprocess.run") as execute:
            with self.assertRaisesRegex(ValueError, "checksum/recipe mismatch"):
                checked_binary(self.root)
            execute.assert_not_called()

    def test_invalid_workflow_expression_is_rejected_by_actual_tool(self) -> None:
        path = self.root / "invalid.yml"
        path.write_text(
            "name: Negative\non: [push]\njobs:\n  bad:\n    runs-on: ubuntu-24.04\n    steps:\n      - run: echo invalid\n        if: ${{ nonexistent.foo }}\n"
        )
        with self.assertRaisesRegex(ValueError, "workflow validation failed"):
            check(ROOT, workflows=[path])

    def test_corrupt_download_cannot_prepare_a_validator(self) -> None:
        with patch("scripts.check_ci.urllib.request.urlopen") as download:
            download.return_value.__enter__.return_value.read.return_value = b"corrupted-archive"
            with self.assertRaisesRegex(ValueError, "archive checksum/size mismatch"):
                setup(self.root)
        self.assertFalse(any((self.root / "build").rglob("actionlint")))


if __name__ == "__main__":
    unittest.main(verbosity=2)
