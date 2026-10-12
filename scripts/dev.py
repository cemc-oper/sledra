"""Public development commands; setup is online, daily commands are offline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from test_tools import ROOT, TOOLS, offline_env

UV = os.environ.get("UV", "uv")
BIN = ROOT / "build/bin"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def python_command(*args: str) -> list[str]:
    return [
        UV,
        "run",
        "--locked",
        "--offline",
        "--no-python-downloads",
        "--python",
        os.environ.get("UV_PYTHON", sys.executable),
        "python",
        *args,
    ]


def run(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> None:
    subprocess.run(command, cwd=cwd, env=env or offline_env(), check=True)


def setup() -> None:
    env = dict(os.environ, GOTOOLCHAIN="local", PYTHONDONTWRITEBYTECODE="1", UV_OFFLINE="0")
    print("Online setup: synchronize uv.lock and populate the Go module cache", flush=True)
    run(
        [
            UV,
            "sync",
            "--locked",
            "--no-python-downloads",
            "--python",
            os.environ.get("UV_PYTHON", sys.executable),
        ],
        env=env,
    )
    run(["go", "mod", "download"], cwd=ROOT / "recorder", env=env)
    run([sys.executable, str(ROOT / "scripts/check_ci.py"), "--setup"], env=env)
    print("Setup complete. Daily build/check/test commands disable dependency network queries.")


def provenance(inputs: dict[str, str]) -> dict[str, str]:
    version = os.environ.get("SLEDRA_VERSION", "dev")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]*", version):
        raise ValueError("SLEDRA_VERSION must contain only letters, digits, dots, plus or hyphens")
    commit = "unknown"
    try:
        top = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], cwd=ROOT, stderr=subprocess.DEVNULL, text=True
        ).strip()
        if Path(top).resolve() == ROOT.resolve():
            commit = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
            ).strip()
            if subprocess.check_output(["git", "status", "--porcelain"], cwd=ROOT):
                commit += "-dirty"
    except (OSError, subprocess.SubprocessError):
        pass
    source = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
    return {"software_version": version, "commit": commit, "source_sha256": source}


def build_timestamp() -> str:
    if epoch := os.environ.get("SOURCE_DATE_EPOCH"):
        if not epoch.isdecimal():
            raise ValueError("SOURCE_DATE_EPOCH must be a nonnegative integer timestamp")
        value = datetime.fromtimestamp(int(epoch), timezone.utc)
    else:
        value = datetime.now(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def build(*, directory: Path = ROOT / "build", tools: tuple[str, ...] = TOOLS) -> dict:
    env = offline_env()
    target = subprocess.check_output(
        ["go", "env", "GOVERSION", "GOOS", "GOARCH"], cwd=ROOT / "recorder", env=env, text=True
    ).splitlines()
    inputs = [
        p
        for p in (ROOT / "recorder").rglob("*")
        if p.is_file() and (p.suffix in {".go", ".json"} or p.name in {"go.mod", "go.sum"})
    ]
    input_hashes = {str(p.relative_to(ROOT)): digest(p) for p in sorted(inputs)}
    metadata = provenance(input_hashes)
    timestamp = build_timestamp()
    recipe = {
        "target": target,
        "cgo_enabled": "0",
        "flags": ["-trimpath", "-buildvcs=false"],
        "inputs": input_hashes,
        "provenance": metadata,
        "source_date_epoch": os.environ.get("SOURCE_DATE_EPOCH"),
        "tools": tools,
    }
    # JSON records tuples as arrays; keep the in-memory recipe comparable.
    recipe["tools"] = list(tools)
    manifest_path = directory / "manifest.json"
    bin_dir = directory / "bin"
    try:
        previous = json.loads(manifest_path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        previous = {}
    if previous.get("recipe") == recipe:
        timestamp = previous.get("build_time", timestamp)
    metadata = dict(metadata, build_time=timestamp)
    symbols = {
        "Version": "software_version",
        "Commit": "commit",
        "BuildTime": "build_time",
        "SourceSHA256": "source_sha256",
    }
    ldflags = " ".join(
        f"-X github.com/cemc-oper/sledra/recorder/internal/buildinfo.{name}={metadata[key]}"
        for name, key in symbols.items()
    )
    bin_dir.mkdir(parents=True, exist_ok=True)
    binaries = {}
    for name in tools:
        binary = bin_dir / name
        if (
            previous.get("recipe") == recipe
            and binary.is_file()
            and os.access(binary, os.X_OK)
            and previous.get("binaries", {}).get(name) == digest(binary)
        ):
            print(f"Reuse {binary.relative_to(ROOT)}", flush=True)
        else:
            print(f"Build {binary.relative_to(ROOT)}", flush=True)
            temporary = bin_dir / f".{name}-{uuid.uuid4().hex}.tmp"
            try:
                run(
                    [
                        "go",
                        "build",
                        *recipe["flags"],
                        "-ldflags",
                        ldflags,
                        "-o",
                        str(temporary),
                        f"./cmd/{name}",
                    ],
                    cwd=ROOT / "recorder",
                    env=env,
                )
                temporary.replace(binary)
            finally:
                temporary.unlink(missing_ok=True)
        binaries[name] = digest(binary)
    manifest = {"recipe": recipe, "build_time": timestamp, "ldflags": ldflags, "binaries": binaries}
    write_json(manifest_path, manifest)
    return manifest


def protected_files() -> dict[str, str]:
    directories = (
        "spec/1.0.0",
        "spec/fixtures",
        "recorder/event/contract",
        "recorder/internal/speccontract/contract",
        "examples/standalone/fixtures",
        "tests/m1/results",
        "tests/platform/results",
    )
    return {
        str(path.relative_to(ROOT)): digest(path)
        for directory in directories
        for path in sorted((ROOT / directory).rglob("*"))
        if path.is_file()
    }


class Report:
    def __init__(self, action: str) -> None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.directory = ROOT / "test-results" / f"{action}-{stamp}-{uuid.uuid4().hex[:8]}"
        self.directory.mkdir(parents=True)
        self.stages: list[dict] = []
        self.action = action

    def stage(
        self, name: str, command: list[str], *, cwd: Path = ROOT, empty_output: bool = False
    ) -> None:
        log = self.directory / f"{name}.log"
        start = time.monotonic()
        print(f"[{name}] start", flush=True)
        with log.open("w") as output:
            result = subprocess.run(
                command,
                cwd=cwd,
                env=dict(offline_env(), SLEDRA_TEST_BIN_DIR=str(BIN)),
                stdout=output,
                stderr=subprocess.STDOUT,
            )
        code = result.returncode
        if empty_output and log.stat().st_size:
            code = 1
        self.stages.append(
            {
                "name": name,
                "command": command,
                "cwd": str(cwd),
                "exit_code": code,
                "seconds": round(time.monotonic() - start, 3),
                "log": log.name,
            }
        )
        print(
            f"[{name}] {'pass' if code == 0 else 'FAIL'} ({self.stages[-1]['seconds']}s)",
            flush=True,
        )
        if code:
            print(log.read_text(), file=sys.stderr)
            raise RuntimeError(f"{name} failed; see {log}")

    def finish(self, error: str | None, before: dict[str, str]) -> bool:
        after = protected_files()
        changed = sorted(
            name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
        )
        if changed:
            error = f"protected fixture bytes changed: {', '.join(changed)}; " + (error or "")
        write_json(
            self.directory / "summary.json",
            {
                "action": self.action,
                "ok": error is None,
                "error": error,
                "stages": self.stages,
                "python_bootstrap": platform.python_version(),
                "platform": platform.platform(),
                "uv_lock_sha256": digest(ROOT / "uv.lock"),
                "go_sum_sha256": digest(ROOT / "recorder/go.sum"),
                "protected_files_before": before,
                "changed_protected_files": changed,
                "handoff_sha256": digest(ROOT / "tests/m1/results/handoff.json"),
                "dependency_network_queries": "disabled: uv offline, GOPROXY/GOSUMDB off, GOTOOLCHAIN local",
            },
        )
        print(f"Report: {self.directory / 'summary.json'}", flush=True)
        if error:
            print(error, file=sys.stderr)
        return error is None


def suite(action: str) -> bool:
    report = Report(action)
    before = protected_files()
    error = None
    try:
        if action == "check":
            report.stage("lock", [UV, "lock", "--check", "--offline", "--no-python-downloads"])
            report.stage(
                "python-format", python_command("-m", "ruff", "format", "--check", "scripts")
            )
            report.stage("python-lint", python_command("-m", "ruff", "check", "."))
            go_files = [str(path) for path in sorted((ROOT / "recorder").rglob("*.go"))]
            report.stage("go-format", ["gofmt", "-l", *go_files], empty_output=True)
            report.stage("go-vet", ["go", "vet", "./..."], cwd=ROOT / "recorder")
            for path in sorted(ROOT.glob("**/*.sh")):
                if not {"build", "test-results", ".venv"}.intersection(
                    path.relative_to(ROOT).parts
                ):
                    report.stage(
                        "bash-" + "-".join(path.relative_to(ROOT).parts), ["bash", "-n", str(path)]
                    )
            report.stage("generated-contracts", python_command("scripts/check_contracts.py"))
            report.stage("docs", python_command("scripts/check_docs.py"))
            report.stage("workflow", [sys.executable, str(ROOT / "scripts/check_ci.py")])
        elif action == "package":
            report.stage("package", [sys.executable, str(ROOT / "scripts/package_trial.py")])
        elif action == "test-package":
            report.stage(
                "isolated-package",
                [
                    sys.executable,
                    str(ROOT / "tests/package/verify.py"),
                    "--report-dir",
                    str(report.directory / "acceptance"),
                ],
            )
        else:
            report.stage("build", [sys.executable, str(ROOT / "scripts/dev.py"), "build"])
            if action == "test-platform":
                report.stage(
                    "platform",
                    python_command(
                        "tests/platform/probe.py",
                        "--bin",
                        str(BIN / "sledra"),
                        "--work-root",
                        str(report.directory),
                        "--report-dir",
                        str(report.directory / "primitives"),
                        "--samples",
                        "10",
                    ),
                )
                report.stage(
                    "cross-node-simulation",
                    python_command(
                        "tests/platform/cross_node.py",
                        "simulate",
                        "--bin",
                        str(BIN / "sledra"),
                        "--work-root",
                        str(report.directory),
                        "--report-dir",
                        str(report.directory / "cross-node"),
                        "--samples",
                        "8",
                    ),
                )
            if action == "test":
                report.stage("go-tests", ["go", "test", "-count=1", "./..."], cwd=ROOT / "recorder")
                report.stage(
                    "engineering",
                    python_command("-m", "unittest", "discover", "-s", "tests/engineering", "-v"),
                )
                report.stage(
                    "package-unit",
                    python_command("-m", "unittest", "discover", "-s", "tests/package", "-v"),
                )
                report.stage(
                    "platform-harness",
                    python_command("-m", "unittest", "discover", "-s", "tests/platform", "-v"),
                )
                for name, script in (
                    ("contract", "spec/verify.py"),
                    ("event", "spec/verify_event.py"),
                    ("context", "spec/verify_context.py"),
                    ("cli", "tests/cli/verify.py"),
                    ("shell", "tests/shell/verify.py"),
                    ("m1", "tests/m1/verify.py"),
                ):
                    report.stage(name, python_command(script))
                report.stage(
                    "frozen-replay",
                    python_command("tests/m1/replay.py", "tests/m1/results/handoff.json"),
                )
            elif action == "demo":
                handoff = report.directory / "handoff.json"
                report.stage(
                    "normal-failure-retry",
                    python_command("tests/m1/verify.py", "--handoff-out", str(handoff)),
                )
                report.stage("demo-replay", python_command("tests/m1/replay.py", str(handoff)))
            elif action == "benchmark":
                report.stage(
                    "benchmark",
                    python_command(
                        "tests/m1/benchmark.py",
                        "--samples",
                        str(int(os.environ.get("SLEDRA_BENCHMARK_SAMPLES", "20"))),
                        "--out",
                        str(report.directory / "benchmark.json"),
                    ),
                )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        error = str(exc)
    return report.finish(error, before)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=(
            "help",
            "setup",
            "build",
            "check",
            "test",
            "demo",
            "benchmark",
            "package",
            "test-package",
            "test-platform",
        ),
    )
    action = parser.parse_args().action
    if sys.version_info < (3, 12):
        parser.error("development commands require Python >=3.12")
    if action == "help":
        print(
            "Sledra development targets (run from repository root):\n"
            "  make setup      Online: sync uv.lock, Go modules and pinned CI validator\n"
            "  make build      Offline: build/reuse five tools in build/bin; build/manifest.json\n"
            "  make check      Offline: lock, format, lint, vet, Bash, contract bytes, docs, workflow\n"
            "  make test       Offline: Go, engineering, contract, portable CLI, Shell, M1, frozen replay\n"
            "  make demo       Offline: isolated synthetic normal/failure/retry, export and replay\n"
            "  make benchmark  Offline: separate raw timing samples and statistics (default 20)\n"
            "  make test-platform Single-node primitives and local two-role simulation\n"
            "  make package    Offline: build a Linux trial archive, manifest and checksums\n"
            "  make test-package Verify the existing archive without source, Go or runtime network\n"
            "Dependencies: Python >=3.12, uv >=0.11.2, Go >=1.25.0, Bash >=5.2, GNU Make >=4.0\n"
            "Reports/demos/benchmarks: unique directories under test-results/; trial archives in dist/\n"
            "Offline source builds require setup caches. Package tests also require bwrap and ldd."
        )
        return 0
    try:
        if action == "setup":
            setup()
        elif action == "build":
            build()
        else:
            return int(not suite(action))
    except (OSError, ValueError, OverflowError, subprocess.SubprocessError) as exc:
        print(f"{action} failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
