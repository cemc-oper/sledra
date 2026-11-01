"""The target preflight preserves data and never claims platform qualification."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "examples/hpc/child-events-preflight.sh"


class ChildEventsPreflight(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sledra-ce07-", dir=Path.home() / ".cache")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.install = self.work / "installation with spaces"
        self.shared = self.work / "shared parent"
        self.runtime = self.work / "runtime"
        self.report = self.work / "report"
        for path in (self.install, self.shared, self.runtime):
            path.mkdir(mode=0o700)
        libraries = self.install / "share/sledra/integrations/bash"
        libraries.mkdir(parents=True)
        for name in ("sledra-execution.sh", "sledra-events.sh"):
            (libraries / name).write_text("# inert preflight fixture; no functional recording\n")
        (self.install / "manifest.json").write_text("{}\n")
        (self.install / "bin").mkdir()
        self.binary = self.install / "bin/sledra"
        self.binary.write_text(
            '#!/usr/bin/env bash\nif [[ $1 == --version ]]; then printf "{}\\n"; '
            'else printf "CE-1\\texecution-child-events\\t1\\n"; fi\n'
        )
        self.binary.chmod(0o755)
        self.checksums()

    def checksums(self):
        files = sorted(p for p in self.install.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
        (self.install / "SHA256SUMS").write_text(
            "".join(
                f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(self.install)}\n"
                for p in files
            )
        )

    def run_probe(self, *, runtime=None, report=None):
        env = {k: v for k, v in os.environ.items() if k not in {"BASH_ENV", "ENV"}}
        return subprocess.run(
            [
                "bash",
                str(SCRIPT),
                str(self.install),
                str(self.shared),
                str(runtime or self.runtime),
                str(report or self.report),
            ],
            env=env,
            capture_output=True,
            timeout=15,
        )

    def test_collects_prerequisites_without_records_or_qualification(self):
        before = {str(p): p.read_bytes() for p in self.install.rglob("*") if p.is_file()}
        result = self.run_probe()
        self.assertEqual(result.returncode, 0, result.stderr)
        log = (self.report / "preflight.log").read_text()
        for expected in (
            "status=preflight_passed",
            "a15=unverified",
            "runtime_node_local=unverified",
            "shared_visibility=unverified",
            "boot_id=",
            "pid_namespace_inode=",
        ):
            self.assertIn(expected, log)
        self.assertEqual(list(self.runtime.iterdir()), [])
        self.assertEqual(list(self.shared.iterdir()), [])
        self.assertEqual(
            before, {str(p): p.read_bytes() for p in self.install.rglob("*") if p.is_file()}
        )
        self.assertEqual(self.report.stat().st_mode & 0o777, 0o700)
        self.assertTrue(all(p.stat().st_mode & 0o077 == 0 for p in self.report.iterdir()))

    def test_rejects_bad_permissions_symlinks_and_writable_ancestors(self):
        self.runtime.chmod(0o755)
        self.assertNotEqual(self.run_probe().returncode, 0)
        self.runtime.chmod(0o700)
        link = self.work / "link"
        link.symlink_to(self.runtime, target_is_directory=True)
        self.assertNotEqual(
            self.run_probe(runtime=link, report=self.work / "link-report").returncode, 0
        )
        with tempfile.TemporaryDirectory(prefix="sledra-ce07-tmp-") as tmp:
            Path(tmp).chmod(0o700)
            self.assertNotEqual(
                self.run_probe(runtime=Path(tmp), report=self.work / "tmp-report").returncode, 0
            )

    def test_rejects_tampered_installation_and_inexact_capability(self):
        self.binary.write_text(self.binary.read_text() + "# tampered\n")
        self.assertNotEqual(self.run_probe().returncode, 0)
        for index, reply in enumerate(
            (
                "CE-1\\texecution-child-events\\t0\\n",
                "CE-1\\texecution-child-events\\t1",
                "CE-1\\texecution-child-events\\t1\\n\\n",
            )
        ):
            self.binary.write_text(f'#!/usr/bin/env bash\nprintf "{reply}"\n')
            self.checksums()
            self.assertNotEqual(self.run_probe(report=self.work / f"bad-{index}").returncode, 0)

    def test_refuses_existing_reports_and_overlapping_paths(self):
        self.report.mkdir()
        (self.report / "sentinel").write_text("keep\n")
        self.assertNotEqual(self.run_probe().returncode, 0)
        self.assertEqual(list(self.report.iterdir()), [self.report / "sentinel"])
        self.assertEqual((self.report / "sentinel").read_text(), "keep\n")
        for report in (self.install / "new-report", self.runtime / "new-report"):
            self.assertNotEqual(self.run_probe(report=report).returncode, 0)
            self.assertFalse(report.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
