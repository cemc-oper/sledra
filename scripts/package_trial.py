"""Build a local Linux trial archive; no public release or license selection."""

from __future__ import annotations

import gzip
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from dev import ROOT, build, write_json
from package_tools import elf_info, sha, verify_install
from test_tools import offline_env

TOOLS = ("sledra", "sledra-spec-check", "sledra-event")


def copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    target.chmod(0o755 if os.access(source, os.X_OK) and source.suffix != ".py" else 0o644)


def main() -> None:
    target = subprocess.check_output(
        ["go", "env", "GOOS", "GOARCH"], cwd=ROOT / "recorder", env=offline_env(), text=True
    ).splitlines()
    goos, arch = target
    if goos != "linux" or arch not in {"amd64", "arm64"}:
        raise ValueError("trial packaging supports GOOS=linux, GOARCH=amd64 or arm64")
    output = ROOT / f"build/package/linux-{arch}"
    build_manifest = build(directory=output, tools=TOOLS)
    provenance = build_manifest["recipe"]["provenance"]
    name = f"sledra-{provenance['software_version']}-linux-{arch}"
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".sledra-package-", dir=dist) as tmp:
        stage = Path(tmp) / name
        share = stage / "share/sledra"
        copy_file(output / "bin/sledra", stage / "bin/sledra")
        for tool in TOOLS[1:]:
            copy_file(output / "bin" / tool, share / "test-tools/bin" / tool)
        for directory in (
            "shell",
            "spec/1.0.0",
            "spec/fixtures/1.0.0",
            "examples/standalone",
            "examples/hpc",
        ):
            for path in sorted((ROOT / directory).rglob("*")):
                if path.is_file() and path.suffix != ".pyc":
                    copy_file(path, share / path.relative_to(ROOT))
        for name_in, name_out in (
            ("scripts/test_tools.py", "scripts/test_tools.py"),
            ("scripts/test_session.py", "scripts/test_session.py"),
            ("scripts/package_tools.py", "scripts/package_tools.py"),
            ("tests/m1/benchmark.py", "tests/m1/benchmark.py"),
            ("tests/shell/verify.py", "tests/shell/verify.py"),
            ("tests/platform/probe.py", "tests/platform/probe.py"),
            ("tests/platform/shell_probe.sh", "tests/platform/shell_probe.sh"),
            ("tests/platform/cross_node.py", "tests/platform/cross_node.py"),
            ("docs/source/testing-hpc.md", "docs/testing-hpc.md"),
            ("scripts/package_tools.py", "test-tools/package_tools.py"),
            ("tests/package/verify_install.py", "test-tools/verify.py"),
            ("tests/m1/verify.py", "tests/m1/verify.py"),
            ("tests/m1/replay.py", "tests/m1/replay.py"),
            ("tests/m1/results/handoff.json", "tests/m1/results/handoff.json"),
            ("packaging/runtime.md", "docs/runtime.md"),
        ):
            copy_file(ROOT / name_in, share / name_out)
        copy_file(ROOT / "packaging/README.trial.md", stage / "README.md")
        # Preserve actual third-party license files from the pinned local cache.
        linked = set()
        for tool in TOOLS:
            information = subprocess.check_output(
                ["go", "version", "-m", str(output / "bin" / tool)], env=offline_env(), text=True
            )
            for line in information.splitlines():
                fields = line.split()
                if fields and fields[0] == "dep":
                    linked.add(fields[1])
        modules = subprocess.check_output(
            ["go", "list", "-m", "-json", *sorted(linked)],
            cwd=ROOT / "recorder",
            env=offline_env(),
            text=True,
        )
        decoder = json.JSONDecoder()
        dependencies = []
        while modules.strip():
            module, end = decoder.raw_decode(modules.lstrip())
            modules = modules.lstrip()[end:]
            if module.get("Main"):
                continue
            licenses = []
            for license_name in ("LICENSE", "NOTICE", "PATENTS"):
                source = Path(module["Dir"]) / license_name
                if source.is_file():
                    destination = share / "licenses" / module["Path"] / license_name
                    copy_file(source, destination)
                    licenses.append(destination.relative_to(stage).as_posix())
            if not licenses:
                raise ValueError(f"no license material found for {module['Path']}")
            dependencies.append(
                {"module": module["Path"], "version": module["Version"], "licenses": licenses}
            )
        goroot = subprocess.check_output(
            ["go", "env", "GOROOT"], env=offline_env(), text=True
        ).strip()
        copy_file(Path(goroot) / "LICENSE", share / "licenses/Go/LICENSE")
        write_json(
            share / "licenses/THIRD_PARTY.json",
            {
                "dependencies": dependencies,
                "go": {"version": build_manifest["recipe"]["target"][0], "license": "Go/LICENSE"},
                "sledra_license": "undecided; local trial only; no public release approval",
            },
        )
        elf = {
            p.relative_to(stage).as_posix(): elf_info(p)
            for p in (stage / "bin/sledra", *(share / "test-tools/bin" / t for t in TOOLS[1:]))
        }
        if any(not info["static"] or info["arch"] != arch for info in elf.values()):
            raise ValueError(
                "trial binaries must actually be static ELF for the named architecture"
            )
        metadata = dict(
            provenance,
            build_time=build_manifest["build_time"],
            go_version=build_manifest["recipe"]["target"][0],
            goos=goos,
            goarch=arch,
            contract_version="1.0.0",
        )
        manifest = {
            "format_version": 1,
            "kind": "local_trial",
            "name": name,
            "public_release": False,
            "sledra_license": "undecided",
            "target": {
                "os": goos,
                "arch": arch,
                "runtime_validation": "unverified; consult separate acceptance report",
            },
            "version": metadata,
            "build": build_manifest,
            "elf": elf,
            "runtime": {
                "recording": ["target binary", "Bash >=5.2"],
                "synthetic_example": ["dirname", "cat", "sleep"],
                "verification": ["Python >=3.12 standard library"],
                "retention_tool": "not included; fixture-only, not production cleanup",
            },
            "files": {
                p.relative_to(stage).as_posix(): {
                    "sha256": sha(p),
                    "size": p.stat().st_size,
                    "mode": f"{p.stat().st_mode & 0o777:04o}",
                }
                for p in sorted(stage.rglob("*"))
                if p.is_file()
            },
        }
        write_json(stage / "manifest.json", manifest)
        paths = sorted(p for p in stage.rglob("*") if p.is_file())
        (stage / "SHA256SUMS").write_text(
            "".join(f"{sha(p)}  {p.relative_to(stage).as_posix()}\n" for p in paths)
        )
        verify_install(stage, native=False)
        archive = Path(tmp) / f"{name}.tar.gz"
        with (
            archive.open("wb") as raw,
            gzip.GzipFile(filename="", fileobj=raw, mode="wb", mtime=0) as compressed,
        ):
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for path in [stage, *sorted(stage.rglob("*"))]:
                    entry = tar.gettarinfo(
                        str(path),
                        arcname=name
                        if path == stage
                        else f"{name}/{path.relative_to(stage).as_posix()}",
                    )
                    entry.uid = entry.gid = 0
                    entry.uname = entry.gname = ""
                    entry.mtime = 0
                    entry.mode = 0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644
                    if path.is_file():
                        with path.open("rb") as data:
                            tar.addfile(entry, data)
                    else:
                        tar.addfile(entry)
        checksum = sha(archive)
        final = dist / archive.name
        archive.replace(final)
        write_json(dist / f"{name}.manifest.json", manifest)
        (dist / f"{name}.tar.gz.sha256").write_text(f"{checksum}  {final.name}\n")
        write_json(
            dist / "latest-package.json",
            {"archive": final.name, "sha256": checksum, "manifest": f"{name}.manifest.json"},
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "archive": str(final),
                    "sha256": checksum,
                    "files": len(manifest["files"]),
                    "target": manifest["target"],
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
