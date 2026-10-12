"""Negative coverage for archive extraction, integrity and target/linkage checks."""

from __future__ import annotations

import io
import json
import struct
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from package_tools import elf_info, sha, unpack, verify_install  # noqa: E402


def elf(*, machine: int = 62, interpreter: bool = False, needed: bool = False) -> bytes:
    ident = b"\x7fELF\x02\x01\x01" + bytes(9)
    count = int(interpreter) + int(needed)
    header = struct.pack(
        "<16sHHIQQQIHHHHHH", ident, 2, machine, 1, 0, 64, 0, 0, 64, 56, count, 0, 0, 0
    )
    programs = b""
    data = b""
    if interpreter:
        programs += struct.pack("<IIQQQQQQ", 3, 0, 0, 0, 0, 0, 0, 0)
    if needed:
        programs += struct.pack("<IIQQQQQQ", 2, 0, 64 + count * 56, 0, 0, 32, 32, 0)
        data = struct.pack("<qQqQ", 1, 0, 0, 0)
    return header + programs + data


def fixture(root: Path) -> None:
    binary = root / "bin/sledra"
    binary.parent.mkdir()
    binary.write_bytes(elf())
    binary.chmod(0o755)
    manifest = {
        "format_version": 1,
        "kind": "local_trial",
        "target": {"os": "linux", "arch": "amd64"},
        "files": {
            "bin/sledra": {"sha256": sha(binary), "size": binary.stat().st_size, "mode": "0755"}
        },
        "elf": {"bin/sledra": elf_info(binary)},
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    (root / "SHA256SUMS").write_text(
        f"{sha(binary)}  bin/sledra\n{sha(root / 'manifest.json')}  manifest.json\n"
    )


class TrialChecks(unittest.TestCase):
    def test_archive_rejects_traversal_links_duplicates_and_file_parents(self) -> None:
        for names, symlink in (
            (["sledra-dev/../escape"], False),
            (["sledra-dev/link"], True),
            (["sledra-dev/file", "sledra-dev/file"], False),
            (["sledra-dev/file", "sledra-dev/file/child"], False),
        ):
            with self.subTest(names=names), tempfile.TemporaryDirectory() as tmp:
                base = Path(tmp)
                archive = base / "trial.tar.gz"
                with tarfile.open(archive, "w:gz") as stream:
                    for name in names:
                        entry = tarfile.TarInfo(name)
                        if symlink:
                            entry.type = tarfile.SYMTYPE
                            entry.linkname = "/etc/passwd"
                            stream.addfile(entry)
                        else:
                            entry.size = 1
                            stream.addfile(entry, io.BytesIO(b"x"))
                with self.assertRaises(ValueError):
                    unpack(archive, base / "unpacked")
                self.assertFalse((base / "unpacked").exists())

    def test_missing_extra_changed_files_and_manifest_are_detected(self) -> None:
        for mutation in ("missing", "extra", "bytes", "manifest", "mode"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                fixture(root)
                verify_install(root, native=False)
                binary = root / "bin/sledra"
                if mutation == "missing":
                    binary.unlink()
                elif mutation == "extra":
                    (root / "unexpected").write_text("x")
                elif mutation == "bytes":
                    binary.write_bytes(binary.read_bytes() + b"x")
                elif mutation == "manifest":
                    with (root / "manifest.json").open("a") as target:
                        target.write(" ")
                else:
                    binary.chmod(0o644)
                with self.assertRaises(ValueError):
                    verify_install(root, native=False)

    def test_cross_target_is_rejected_before_execution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fixture(root)
            with patch("package_tools.host_arch", return_value="arm64"):
                with self.assertRaisesRegex(ValueError, "cross-build is unverified"):
                    verify_install(root)

    def test_actual_elf_linkage_not_cgo_label_is_checked(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            binary = Path(tmp) / "sledra"
            for interpreter, needed in ((True, False), (False, True)):
                binary.write_bytes(elf(interpreter=interpreter, needed=needed))
                self.assertFalse(elf_info(binary)["static"])
            binary.write_bytes(elf())
            self.assertTrue(elf_info(binary)["static"])
            binary.write_bytes(elf(machine=183))
            self.assertEqual(elf_info(binary)["arch"], "arm64")
            binary.write_bytes(b"not ELF")
            with self.assertRaisesRegex(ValueError, "ELF64"):
                elf_info(binary)


if __name__ == "__main__":
    unittest.main(verbosity=2)
