"""Standard-library integrity and ELF checks shared with the installed verifier."""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import struct
import tarfile
from pathlib import Path, PurePosixPath

MACHINES = {62: "amd64", 183: "arm64"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def relative(name: str) -> Path:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts or str(path) != name or "\\" in name:
        raise ValueError(f"invalid package path: {name}")
    return Path(*path.parts)


def elf_info(path: Path) -> dict:
    data = path.read_bytes()
    if len(data) < 64 or data[:6] != b"\x7fELF\x02\x01":
        raise ValueError(f"expected little-endian ELF64: {path.name}")
    header = struct.unpack_from("<16sHHIQQQIHHHHHH", data)
    machine = MACHINES.get(header[2])
    offset, size, count = header[5], header[9], header[10]
    if machine is None or size < 56 or count > 4096 or offset + size * count > len(data):
        raise ValueError(f"invalid/unsupported ELF headers: {path.name}")
    interpreter = False
    needed = 0
    for index in range(count):
        program = struct.unpack_from("<IIQQQQQQ", data, offset + index * size)
        if program[0] == 3:
            interpreter = True
        if program[0] == 2:
            start, length = program[2], program[5]
            if start + length > len(data) or length % 16:
                raise ValueError(f"invalid ELF dynamic table: {path.name}")
            for pos in range(start, start + length, 16):
                tag, _ = struct.unpack_from("<qQ", data, pos)
                if tag == 0:
                    break
                needed += tag == 1
    return {
        "class": "ELF64",
        "arch": machine,
        "has_interpreter": interpreter,
        "needed_libraries": needed,
        "static": not interpreter and needed == 0,
    }


def host_arch() -> str:
    machine = platform.machine().lower()
    return {"x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(machine, machine)


def verify_install(root: Path, *, native: bool = True) -> dict:
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("format_version") != 1 or manifest.get("kind") != "local_trial":
        raise ValueError("unsupported package manifest")
    files = manifest["files"]
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("package contains a link or special file")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual != set(files) | {"manifest.json", "SHA256SUMS"}:
        raise ValueError("package file inventory differs from manifest")
    for name, entry in files.items():
        path = root / relative(name)
        if path.stat().st_size != entry["size"] or sha(path) != entry["sha256"]:
            raise ValueError(f"package checksum mismatch: {name}")
        if path.stat().st_mode & 0o111 != int(entry["mode"], 8) & 0o111:
            raise ValueError(f"package executable mode mismatch: {name}")
    checksums = {}
    for line in (root / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        relative(name)
        if name in checksums:
            raise ValueError("duplicate package checksum path")
        checksums[name] = digest
    if set(checksums) != set(files) | {"manifest.json"}:
        raise ValueError("package checksum inventory differs")
    for name, digest in checksums.items():
        if sha(root / name) != digest:
            raise ValueError(f"package SHA256SUMS mismatch: {name}")
    target = manifest["target"]
    if target["os"] != "linux" or target["arch"] not in MACHINES.values():
        raise ValueError("unsupported package target")
    for name, expected in manifest["elf"].items():
        actual_elf = elf_info(root / relative(name))
        if (
            actual_elf != expected
            or not actual_elf["static"]
            or actual_elf["arch"] != target["arch"]
        ):
            raise ValueError(f"package binary linkage/architecture mismatch: {name}")
    if native and (platform.system() != "Linux" or host_arch() != target["arch"]):
        raise ValueError(
            f"package architecture {target['arch']} cannot run on {platform.system()}/{host_arch()}; cross-build is unverified"
        )
    return manifest


def unpack(archive: Path, destination: Path) -> Path:
    """Extract only ordinary files/directories into a newly created directory."""
    if archive.stat().st_size > 128 << 20:
        raise ValueError("trial archive exceeds compressed size limit")
    with tarfile.open(archive, "r:gz") as stream:
        members = stream.getmembers()
        if not members or len(members) > 2000 or sum(m.size for m in members) > 256 << 20:
            raise ValueError("trial archive inventory/size limit exceeded")
        names = set()
        tops = set()
        for member in members:
            path = relative(member.name)
            if member.name in names or not (member.isfile() or member.isdir()):
                raise ValueError("duplicate, link or special archive member")
            if member.mode & 0o7022 or member.size < 0:
                raise ValueError("unsafe archive permissions/size")
            names.add(member.name)
            tops.add(path.parts[0])
        if len(tops) != 1 or not next(iter(tops)).startswith("sledra-"):
            raise ValueError("trial archive must contain one sledra directory")
        # Reject a regular file that is also the parent of another member.
        file_names = {m.name for m in members if m.isfile()}
        for member in members:
            if any(str(p) in file_names for p in PurePosixPath(member.name).parents):
                raise ValueError("archive file/directory collision")
        destination.mkdir(parents=True, exist_ok=False)
        for member in members:
            path = destination / relative(member.name)
            path.parent.mkdir(parents=True, exist_ok=True)
            if member.isdir():
                path.mkdir(exist_ok=True)
            else:
                with stream.extractfile(member) as source, path.open("xb") as output:
                    shutil.copyfileobj(source, output)
                path.chmod(member.mode & 0o777)
        return destination / next(iter(tops))


def readonly(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("cannot freeze a symlink installation")
        path.chmod(0o555 if path.is_dir() or path.stat().st_mode & 0o111 else 0o444)
    root.chmod(0o555)
