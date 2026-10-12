"""Extract and accept an existing trial in a source-free network namespace."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from package_tools import readonly, sha, unpack, verify_install  # noqa: E402

INSTALL = "/opt/sledra installation"


def sandbox(install: Path, work: Path, *, python: bool = False) -> list[str]:
    bwrap = shutil.which("bwrap")
    if not bwrap or not shutil.which("ldd"):
        raise ValueError("isolated package acceptance requires bwrap and ldd; no host fallback")
    command = [
        bwrap,
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
        "--new-session",
        "--clearenv",
        "--ro-bind",
        str(install),
        INSTALL,
        "--bind",
        str(work),
        "/work",
        "--proc",
        "/proc",
        "--dev",
        "/dev",
        "--tmpfs",
        "/tmp",
    ]
    binaries = {
        f"/runtime/bin/{name}": Path(shutil.which(name) or "")
        for name in ("bash", "dirname", "cat", "sleep")
    }
    binaries["/usr/bin/env"] = Path(shutil.which("env") or "")
    if python:
        for name in ("grep", "findmnt"):
            binaries[f"/runtime/bin/{name}"] = Path(shutil.which(name) or "")
        binaries["/usr/bin/python3"] = Path("/usr/bin/python3").resolve()
        stdlib = subprocess.check_output(
            [
                "/usr/bin/python3",
                "-I",
                "-c",
                "import sysconfig; print(sysconfig.get_path('stdlib'))",
            ],
            text=True,
        ).strip()
        command += ["--ro-bind", stdlib, stdlib]
    libraries = set()
    for target, source in binaries.items():
        if not source.is_file():
            raise ValueError(f"missing sandbox system binary: {target}")
        output = subprocess.check_output(["ldd", str(source)], text=True)
        libraries.update(re.findall(r"(/[^\s()]+)", output))
        command += ["--ro-bind", str(source.resolve()), target]
    for library in sorted(libraries):
        command += ["--ro-bind", library, library]
    command += [
        "--setenv",
        "PATH",
        "/runtime/bin",
        "--setenv",
        "HOME",
        "/work",
        "--setenv",
        "TMPDIR",
        "/tmp",
        "--setenv",
        "PYTHONDONTWRITEBYTECODE",
        "1",
        "--setenv",
        "LC_ALL",
        "C",
        "--chdir",
        "/work",
        "--",
    ]
    return command


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, default=os.environ.get("SLEDRA_PACKAGE_ARCHIVE"))
    parser.add_argument("--report-dir", type=Path, required=True)
    args = parser.parse_args()
    report = args.report_dir.resolve()
    report.mkdir(parents=True, exist_ok=False)
    summary = {"ok": False, "commands": [], "source_root": str(ROOT)}
    try:
        archive = args.archive
        if archive is None:
            pointer = json.loads((ROOT / "dist/latest-package.json").read_text())
            archive = ROOT / "dist" / pointer["archive"]
        archive = archive.resolve()
        summary.update(archive=str(archive), archive_sha256=sha(archive))
        digest, filename = Path(str(archive) + ".sha256").read_text().strip().split("  ", 1)
        if filename != archive.name or sha(archive) != digest:
            raise ValueError("archive checksum mismatch")
        install = unpack(archive, report / "unpacked")
        manifest = verify_install(install)
        summary.update(
            target=manifest["target"],
            version=manifest["version"],
            manifest_sha256=sha(install / "manifest.json"),
        )
        readonly(install)
        runtime = report / "runtime"
        runtime.mkdir()
        commands = summary["commands"]

        def execute(name: str, command: list[str]) -> subprocess.CompletedProcess:
            result = subprocess.run(command, capture_output=True, text=True, timeout=120)
            commands.append(
                {
                    "name": name,
                    "command": command,
                    "exit_code": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                }
            )
            if result.returncode:
                raise ValueError(f"{name} failed: {result.stderr}; see summary.json")
            return result

        # This first namespace has no Python executable or stdlib mount.
        smoke = f"""set -euo pipefail
[[ ! -e {shlex.quote(str(ROOT))} && ! -e /usr/local/go && ! -e /usr/bin/python3 ]]
if command -v go; then exit 1; fi
if command -v python3; then exit 1; fi
[[ ! -w {shlex.quote(INSTALL)} ]]
export STANDALONE_WORK_DIR=/work
export SLEDRA_ROOT=/work/observability
export SLEDRA_BIN={shlex.quote(INSTALL)}/bin/sledra SLEDRA_ENABLED=1
{shlex.quote(INSTALL)}/share/sledra/examples/standalone/run.sh
[[ -f /work/observability/contexts/attempt.json ]]
printf 'runtime-without-python-go-source:pass; bash:%s\\n' "$BASH_VERSION"
"""
        (runtime / "observability").mkdir()
        execute(
            "recording-without-python",
            [*sandbox(install, runtime), "/runtime/bin/bash", "-c", smoke],
        )
        # A second namespace permits only the standard-library test interpreter.
        probe = """import errno,json,os,shutil,socket
s=socket.socket();s.settimeout(1)
try:
 s.connect(("192.0.2.1",9));raise RuntimeError("sandbox unexpectedly has network")
except OSError as e:
 assert e.errno==errno.ENETUNREACH,e
assert shutil.which("go") is None
print(json.dumps({"network":"unshared; ENETUNREACH", "network_namespace":os.readlink("/proc/self/ns/net"), "go_on_path":shutil.which("go")}))
"""
        result = execute(
            "isolation-probe",
            [*sandbox(install, runtime, python=True), "/usr/bin/python3", "-c", probe],
        )
        summary["isolation"] = json.loads(result.stdout)
        summary["isolation"]["host_network_namespace"] = os.readlink("/proc/self/ns/net")
        if (
            summary["isolation"]["network_namespace"]
            == summary["isolation"]["host_network_namespace"]
        ):
            raise ValueError("network namespace was not isolated")
        execute(
            "installed-verification",
            [
                *sandbox(install, runtime, python=True),
                "/usr/bin/python3",
                f"{INSTALL}/share/sledra/test-tools/verify.py",
                "--work-root",
                "/work",
                "--report-dir",
                "/work/acceptance",
            ],
        )
        acceptance = json.loads((runtime / "acceptance/summary.json").read_text())
        if not acceptance["ok"]:
            raise ValueError("installed verifier reported failure")
        verify_install(install)
        summary.update(
            ok=True,
            acceptance=acceptance,
            read_only_installation=True,
            source_and_go_unmounted=True,
        )
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AssertionError,
        tarfile.TarError,
        subprocess.SubprocessError,
    ) as exc:
        summary["error"] = str(exc)
        print(str(exc), file=sys.stderr)
    (report / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"ok": summary["ok"], "report": str(report / "summary.json")}))
    return int(not summary["ok"])


if __name__ == "__main__":
    raise SystemExit(main())
