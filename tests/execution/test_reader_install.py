"""Actual wheel resource/reader use without the source tree or legacy services."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.test_tools import binary_for  # noqa: E402


class InstalledExecutionReader(unittest.TestCase):
    def test_wheel_is_independent_read_only_and_preserves_old_resources(self):
        with tempfile.TemporaryDirectory(prefix="sledra reader wheel ") as temp:
            base = Path(temp)
            subprocess.run(
                [
                    "uv",
                    "build",
                    "--offline",
                    "--no-python-downloads",
                    "--out-dir",
                    str(base / "dist"),
                ],
                cwd=ROOT / "analysis",
                capture_output=True,
                check=True,
                timeout=60,
            )
            wheel = next((base / "dist").glob("*.whl"))
            with zipfile.ZipFile(wheel) as archive:
                resource = "sledra_analysis/resources/execution_v2/contract.schema.json"
                self.assertEqual(
                    archive.read(resource),
                    (ROOT / "spec/execution-v2/contract.schema.json").read_bytes(),
                )
                old = ROOT / "analysis/src/sledra_analysis/resources/contract"
                names = {"sledra_analysis/resources/contract/" + p.name for p in old.glob("*.json")}
                self.assertEqual(
                    {
                        n
                        for n in archive.namelist()
                        if n.startswith("sledra_analysis/resources/contract/")
                    },
                    names,
                )
                for path in old.glob("*.json"):
                    self.assertEqual(
                        archive.read("sledra_analysis/resources/contract/" + path.name),
                        path.read_bytes(),
                    )
            root = base / "records"
            root.mkdir()
            (root / "sledra.json").write_bytes(
                (ROOT / "spec/execution-v2/cases/root.json").read_bytes()
            )
            raw = (ROOT / "spec/execution-v2/cases/lifecycle.jsonl").read_bytes()
            # The frozen synthetic stream starts on this entry date. No runtime
            # path or schema from the source directory is passed to the child.
            first = json.loads(raw.splitlines()[0])
            identity = first["execution_id"]
            bucket = hashlib.sha256(identity.encode()).hexdigest()[0]
            binary = binary_for("sledra", base)
            prepared = subprocess.run(
                [
                    str(binary),
                    "execution",
                    "prepare",
                    "event",
                    "example.progress",
                    "--execution-id",
                    identity,
                    "--float",
                    "zero",
                    "0e" + "9" * 40,
                ],
                capture_output=True,
                check=True,
                timeout=10,
            ).stdout
            raw += prepared.split(b"\t", 2)[2]
            path = root / "executions/20261027" / bucket / (identity + ".jsonl")
            path.parent.mkdir(parents=True)
            path.write_bytes(raw)
            before = {
                str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
            }
            wheel.chmod(0o444)
            script = r"""
import os,sys
sys.path.insert(0,sys.argv[1])
def audit(event,args):
    if event=="socket.connect" or event.startswith("sqlite3.connect"):
        raise AssertionError(event)
    if event=="open" and args[2] & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND):
        raise AssertionError("reader attempted a write")
sys.addaudithook(audit)
import sledra_analysis.execution
assert "jsonschema" not in sys.modules
from sledra_analysis.execution.reader import FileReader,Fact
with FileReader(sys.argv[2]) as reader:
    rows=list(reader.iter_events("2026-10-27","2026-10-27"))
assert len(rows)==int(sys.argv[3]),rows
assert all(isinstance(r,Fact) for r in rows)
from decimal import Decimal
assert rows[-1].data["attributes"]["zero"]==Decimal(0)
assert not any(n=="sqlite3" or n.startswith(("sledra_analysis.ingest","sledra_analysis.store","sledra_analysis.projections","sledra_analysis.metrics")) for n in sys.modules)
import sledra_analysis.execution.codec as codec
assert codec.__file__.startswith(sys.argv[1])
print("installed independent reader passed")
"""
            result = subprocess.run(
                [
                    sys.executable,
                    "-I",
                    "-B",
                    "-c",
                    script,
                    str(wheel),
                    str(root),
                    str(len(raw.splitlines())),
                ],
                cwd=base,
                env={k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "BASH_ENV"}},
                capture_output=True,
                timeout=15,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b"installed independent reader passed\n")
            self.assertEqual(
                before,
                {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()},
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
