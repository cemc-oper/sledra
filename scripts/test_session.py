"""Shared source/installed test paths, preflight, reports and owned cleanup."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from scripts.package_tools import elf_info, host_arch, verify_install
from scripts.test_tools import ROOT, binary_for


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--bin", type=Path, help="existing native sledra; never rebuild an override"
    )
    parser.add_argument("--spec-dir", type=Path, help="compatible explicit contract directory")
    parser.add_argument(
        "--work-root", type=Path, help="existing test parent; mandatory in HPC mode"
    )
    parser.add_argument("--report-dir", type=Path, help="new report directory; never overwrite")
    parser.add_argument("--keep-work", action="store_true", help="retain successful work too")
    parser.add_argument("--mode", choices=("local", "hpc"), default="local")


def clean_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("SLEDRA_", "STANDALONE_"))
    } | {"PYTHONDONTWRITEBYTECODE": "1", "LC_ALL": "C"}


def mount_info(path: Path) -> dict:
    # No findmnt executable or Go dependency is needed in an installed namespace.
    matches = []
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        left, right = line.split(" - ", 1)
        fields, extra = left.split(), right.split()
        target = fields[4]
        for old, new in (("\\040", " "), ("\\011", "\t"), ("\\012", "\n"), ("\\134", "\\")):
            target = target.replace(old, new)
        if path.is_relative_to(Path(target)):
            matches.append(
                {
                    "target": target,
                    "source": extra[1],
                    "filesystem": extra[0],
                    "options": fields[5],
                    "super_options": extra[2],
                }
            )
    if not matches:
        raise ValueError(f"cannot identify target mount: {path}")
    result = max(matches, key=lambda item: len(item["target"]))
    result["device"] = path.stat().st_dev
    result["statvfs"] = dict(
        zip(
            (
                "block_size",
                "fragment_size",
                "blocks",
                "blocks_free",
                "blocks_available",
                "files",
                "files_free",
                "files_available",
                "flags",
                "name_max",
            ),
            os.statvfs(path),
        )
    )
    return result


def source_identity() -> dict:
    installation = ROOT.parents[1]
    if (installation / "manifest.json").is_file() and not (ROOT / "recorder").exists():
        manifest = verify_install(installation)
        return {
            "mode": "installed",
            "installation": str(installation),
            "manifest_sha256": digest(installation / "manifest.json"),
            "version": manifest["version"],
            "archive_sha256": "not available from extracted tree; record detached archive checksum separately",
        }
    ignored = {".git", ".venv", "build", "dist", "test-results", "__pycache__", ".ruff_cache"}
    inputs = {
        str(p.relative_to(ROOT)): digest(p)
        for p in sorted(ROOT.rglob("*"))
        if p.is_file() and not ignored.intersection(p.relative_to(ROOT).parts)
    }
    return {
        "mode": "source",
        "inputs": inputs,
        "source_sha256": hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest(),
    }


class TestSession:
    """Reports survive errors; cleanup is restricted to this session's new child."""

    def __init__(self, args: argparse.Namespace, name: str) -> None:
        self.args, self.name = args, name
        self.token = uuid.uuid4().hex
        self.work: Path | None = None
        self.report = (
            args.report_dir or Path.cwd() / "test-results" / f"{name}-{self.token}"
        ).absolute()
        self.report.mkdir(parents=True, exist_ok=False)
        self.commands: list[dict] = []
        self.data = {
            "format_version": 1,
            "kind": name,
            "status": "fail",
            "reason": "preflight incomplete",
            "command": [sys.executable, *sys.argv],
            "mode": args.mode,
            "started_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "node": platform.node(),
            "platform": platform.uname()._asdict(),
            "python": platform.python_version(),
            "sample_count": 0,
            "qualifications": {
                key: {"status": "unverified", "reason": reason}
                for key, reason in (
                    ("real_cross_node", "requires distinct compute nodes and site evidence"),
                    ("hpc_platform", "site acceptance is separate from this synthetic test"),
                    ("power_loss", "process exit does not demonstrate power-loss durability"),
                    ("A6", "budget not approved; raw timing is measurement only"),
                )
            },
        }

    def __enter__(self) -> TestSession:
        try:
            if sys.version_info < (3, 12):
                raise ValueError("verification requires Python >=3.12")
            if self.args.mode == "hpc" and self.args.work_root is None:
                raise ValueError("HPC mode requires explicit --work-root; no /tmp fallback")
            parent = (self.args.work_root or Path(tempfile.gettempdir())).resolve(strict=True)
            if not parent.is_dir():
                raise ValueError("--work-root must be an existing directory")
            self.work = Path(tempfile.mkdtemp(prefix=f"sledra-{self.name}-", dir=parent))
            save(self.work / ".sledra-test-run.json", {"token": self.token, "kind": self.name})
            self.data.update(
                work_root=str(parent), work_directory=str(self.work), mount=mount_info(self.work)
            )
            bash = self.run(["bash", "--version"])
            if bash.returncode or tuple(
                int(x) for x in bash.stdout.split("version ")[1].split("(")[0].split(".")[:2]
            ) < (5, 2):
                raise ValueError("verification requires Bash >=5.2")
            self.data["bash"] = bash.stdout.splitlines()[0]
            for tool in ("dirname", "cat", "sleep"):
                if shutil.which(tool) is None:
                    raise ValueError(f"missing test dependency: {tool}")
            self.data["input"] = source_identity()
            if self.args.bin:
                self.binary = self.args.bin.resolve(strict=True)
                if not self.binary.is_file() or not os.access(self.binary, os.X_OK):
                    raise ValueError(f"--bin is not an executable: {self.binary}")
            else:
                self.binary = binary_for("sledra", self.work)
            self.data["binary"] = {
                "path": str(self.binary),
                "sha256": digest(self.binary),
                "elf": elf_info(self.binary),
            }
            if self.data["binary"]["elf"]["arch"] != host_arch():
                raise ValueError(
                    "binary architecture does not match host; cross-build is unverified"
                )
            result = self.run([str(self.binary), "--version"])
            if result.returncode:
                raise ValueError(f"binary version preflight failed: {result.stderr}")
            self.data["binary"]["version"] = json.loads(result.stdout)
            self.spec = (self.args.spec_dir or ROOT / "spec/1.0.0").resolve(strict=True)
            if not self.spec.is_dir():
                raise ValueError("--spec-dir must be a contract directory")
            self.env = clean_env() | {
                "SLEDRA_BIN": str(self.binary),
                "SLEDRA_TEST_BIN_DIR": str(self.binary.parent),
                "SLEDRA_SPEC_DIR": str(self.spec),
                "TMPDIR": str(self.work),
            }
            self.data["spec"] = {str(p.name): digest(p) for p in sorted(self.spec.glob("*.json"))}
            preflight = self.work / "contract-preflight"
            preflight.mkdir(mode=0o700)
            result = self.run(
                [
                    str(self.binary),
                    "context",
                    "validate",
                    "--file",
                    str(preflight / "missing.json"),
                ],
                env=self.env | {"SLEDRA_ROOT": str(preflight)},
            )
            if result.returncode != 1 or json.loads(result.stderr).get("code") != "context_missing":
                raise ValueError(f"contract preflight failed: {result.stderr.strip()}")
            return self
        except BaseException as exc:
            self.finish(exc)
            raise

    def run(
        self, command: list[str], *, env: dict | None = None, timeout: float = 90, **kwargs
    ) -> subprocess.CompletedProcess:
        start = time.monotonic_ns()
        record = {"argv": command, "cwd": str(kwargs.get("cwd", Path.cwd()))}
        self.commands.append(record)
        try:
            result = subprocess.run(
                command,
                env=env or clean_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
                **kwargs,
            )
            record.update(exit_code=result.returncode, stdout=result.stdout, stderr=result.stderr)
            return result
        except (OSError, subprocess.SubprocessError) as exc:
            record["error"] = str(exc)
            raise
        finally:
            record["elapsed_ns"] = time.monotonic_ns() - start

    def finish(self, error: BaseException | None) -> None:
        cleanup_error = None
        if self.work is not None and not error and not self.args.keep_work:
            try:
                marker = self.work / ".sledra-test-run.json"
                if self.work.is_symlink() or json.loads(marker.read_text())["token"] != self.token:
                    raise ValueError("refusing cleanup of work tree without this run's owner token")
                for path in [self.work, *self.work.rglob("*")]:
                    if path.is_dir() and not path.is_symlink():
                        path.chmod(path.stat().st_mode | 0o700)
                shutil.rmtree(self.work)
            except (OSError, ValueError, KeyError) as exc:
                error = cleanup_error = exc
        self.data.update(
            status="fail" if error else "pass",
            reason=str(error) if error else None,
            work_retained=bool(error or self.args.keep_work),
            commands=len(self.commands),
        )
        save(self.report / "commands.json", self.commands)
        save(self.report / "summary.json", self.data)
        print(f"Report: {self.report / 'summary.json'}", file=sys.stderr)
        if cleanup_error:
            raise cleanup_error

    def __exit__(self, kind, error, traceback) -> None:
        self.finish(error)
