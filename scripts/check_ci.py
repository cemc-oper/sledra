"""Online setup of a pinned actionlint; offline workflow validation thereafter."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.package_tools import elf_info, host_arch  # noqa: E402


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def recipe(root: Path) -> dict:
    config = json.loads((root / ".github/ci-tools.json").read_text())["actionlint"]
    target = f"{platform.system().lower()}-{host_arch()}"
    if target not in config["targets"]:
        raise ValueError(f"no actionlint setup target for {target}")
    return {"version": config["version"], "target": target, **config["targets"][target]}


def checked_binary(root: Path, *, override: Path | None = None) -> tuple[Path, dict]:
    expected = recipe(root)
    directory = root / "build/ci-tools" / expected["target"]
    binary = override or directory / "actionlint"
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise ValueError(f"missing actionlint: {binary}; run make setup (online)")
    if override is None:
        try:
            manifest = json.loads((directory / "manifest.json").read_text())
        except (OSError, ValueError) as exc:
            raise ValueError("missing/invalid actionlint manifest; run make setup") from exc
        license_path = directory / "LICENSE.txt"
        if (
            not isinstance(manifest, dict)
            or manifest.get("recipe") != expected
            or manifest.get("binary_sha256") != sha(binary)
            or not license_path.is_file()
            or manifest.get("license_sha256") != sha(license_path)
        ):
            raise ValueError("actionlint cache checksum/recipe mismatch; run make setup")
    elf = elf_info(binary)
    if elf["arch"] != host_arch():
        raise ValueError("actionlint architecture differs from host")
    result = subprocess.run([str(binary), "-version"], capture_output=True, text=True, timeout=10)
    lines = result.stdout.splitlines()
    if result.returncode or not lines or lines[0].removeprefix("v") != expected["version"]:
        raise ValueError(f"actionlint version must be {expected['version']}")
    return binary, {
        "version": expected["version"],
        "sha256": sha(binary),
        "elf": elf,
        "origin": "explicit local override" if override else expected["url"],
    }


def setup(root: Path = ROOT) -> None:
    expected = recipe(root)
    try:
        binary, _ = checked_binary(root)
        print(f"Reuse {binary.relative_to(root)}")
        return
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        pass
    directory = root / "build/ci-tools" / expected["target"]
    directory.mkdir(parents=True, exist_ok=True)
    print(f"Download actionlint {expected['version']} ({expected['target']})", flush=True)
    # Bound the download and extract only known regular files, never tar paths.
    with urllib.request.urlopen(expected["url"], timeout=60) as response:
        data = response.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != expected["sha256"]:
        raise ValueError("actionlint archive checksum/size mismatch")
    with tempfile.TemporaryDirectory(prefix=".actionlint-", dir=directory) as tmp:
        stage = Path(tmp)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            for name in ("actionlint", "LICENSE.txt"):
                member = archive.getmember(name)
                if not member.isfile() or not 0 < member.size <= 32 * 1024 * 1024:
                    raise ValueError(f"invalid actionlint archive member: {name}")
                (stage / name).write_bytes(archive.extractfile(member).read())
        (stage / "actionlint").chmod(0o755)
        checked_binary(root, override=stage / "actionlint")
        manifest = {
            "recipe": expected,
            "binary_sha256": sha(stage / "actionlint"),
            "license_sha256": sha(stage / "LICENSE.txt"),
        }
        for name in ("actionlint", "LICENSE.txt"):
            shutil.move(str(stage / name), str(directory / name))
        (directory / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n"
        )
    print(f"Prepared {directory.relative_to(root)}")


def check(
    root: Path = ROOT, *, workflows: list[Path] | None = None, override: Path | None = None
) -> dict:
    binary, info = checked_binary(root, override=override)
    paths = (
        workflows if workflows is not None else sorted((root / ".github/workflows").glob("*.y*ml"))
    )
    if not paths:
        raise ValueError("no workflows to validate")
    # Optional shellcheck/pyflakes availability must not change the shared gate.
    command = [str(binary), "-shellcheck=", "-pyflakes=", *map(str, paths)]
    result = subprocess.run(command, cwd=root, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(
            f"workflow validation failed (exit {result.returncode}):\n{result.stdout}{result.stderr}"
        )
    return {
        "ok": True,
        "tool": info,
        "command": command,
        "workflows": {
            str(path.relative_to(root)) if path.is_relative_to(root) else str(path): sha(path)
            for path in paths
        },
        "remote_execution": "unverified; static validation only",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--setup", action="store_true", help="explicit online tool preparation")
    parser.add_argument(
        "--binary", type=Path, help="existing native actionlint of the pinned version"
    )
    parser.add_argument("--workflow", type=Path, action="append", help="validate explicit files")
    args = parser.parse_args()
    try:
        if args.setup:
            setup()
        else:
            print(json.dumps(check(workflows=args.workflow, override=args.binary), sort_keys=True))
    except (
        OSError,
        ValueError,
        KeyError,
        IndexError,
        tarfile.TarError,
        subprocess.SubprocessError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
