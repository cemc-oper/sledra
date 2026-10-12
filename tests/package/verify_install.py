"""Verify an unpacked trial using Python's standard library and bundled tools."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.dont_write_bytecode = True

from package_tools import verify_install, sha  # noqa: E402


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


class Acceptance:
    def __init__(self, package: Path, work: Path, report: Path) -> None:
        self.package = package
        self.share = package / "share/sledra"
        self.work = work
        self.report = report
        self.commands: list[dict] = []
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("SLEDRA_", "STANDALONE_", "PYTHON"))
        }
        temporary = work / "temporary"
        temporary.mkdir()
        self.env.update(
            PYTHONDONTWRITEBYTECODE="1",
            TMPDIR=str(temporary),
            SLEDRA_TEST_BIN_DIR=str(package / "bin"),
        )

    def call(
        self,
        name: str,
        args: list[str],
        *,
        env: dict | None = None,
        code: int = 0,
        quiet: bool = True,
        cwd: Path | None = None,
    ) -> subprocess.CompletedProcess:
        start = time.monotonic()
        result = subprocess.run(
            args, cwd=cwd or self.work, env=env or self.env, capture_output=True, timeout=90
        )
        item = {
            "name": name,
            "command": args,
            "cwd": str(cwd or self.work),
            "exit_code": result.returncode,
            "stdout": result.stdout.decode(errors="replace"),
            "stderr": result.stderr.decode(errors="replace"),
            "seconds": round(time.monotonic() - start, 3),
        }
        self.commands.append(item)
        save(self.report / "commands.json", self.commands)
        if result.returncode != code or (quiet and (result.stdout or result.stderr)):
            raise ValueError(f"{name} failed (exit {result.returncode}); see commands.json")
        return result

    def shell_examples(self) -> None:
        for mode in ("success", "fail"):
            values = []
            for enabled in ("0", "1"):
                work = self.work / f"shell-{mode}-{enabled}"
                (work / "observability").mkdir(parents=True)
                env = dict(
                    self.env,
                    SLEDRA_ENABLED=enabled,
                    SLEDRA_BIN=str(self.package / "bin/sledra"),
                    STANDALONE_WORK_DIR=str(work),
                    STANDALONE_PAYLOAD_MODE=mode,
                )
                result = self.call(
                    f"shell-{mode}-{enabled}",
                    [str(self.share / "examples/standalone/run.sh")],
                    env=env,
                    code=0 if mode == "success" else 7,
                    quiet=False,
                )
                values.append(
                    (
                        result.returncode,
                        result.stdout,
                        result.stderr,
                        (work / "executions.log").read_bytes(),
                        (work / "callbacks.log").read_bytes(),
                        (work / "product.txt").read_bytes()
                        if (work / "product.txt").exists()
                        else None,
                    )
                )
                if (work / "executions.log").read_bytes() != b"run\n":
                    raise ValueError("standalone payload did not execute exactly once")
                if enabled == "1":
                    for name in ("run", "attempt"):
                        self.call(
                            f"shell-{mode}-context-{name}",
                            [
                                str(self.package / "bin/sledra"),
                                "context",
                                "validate",
                                "--file",
                                str(work / f"observability/contexts/{name}.json"),
                            ],
                            env=dict(self.env, SLEDRA_ROOT=str(work / "observability")),
                        )
            if values[0] != values[1]:
                raise ValueError(f"Shell observation changed {mode} business behavior")

    def run(self, manifest: dict) -> dict:
        result = self.call("version", [str(self.package / "bin/sledra"), "--version"], quiet=False)
        if json.loads(result.stdout) != manifest["version"]:
            raise ValueError("binary version differs from package manifest")
        self.call("help", [str(self.package / "bin/sledra"), "--help"], quiet=False)
        self.shell_examples()
        handoff = self.report / "handoff.json"
        common = [
            "--bin",
            str(self.package / "bin/sledra"),
            "--work-root",
            str(self.work),
            "--keep-work",
        ]
        result = self.call(
            "m1-normal-failure-retry",
            [
                sys.executable,
                str(self.share / "tests/m1/verify.py"),
                "--handoff-out",
                str(handoff),
                *common,
                "--report-dir",
                str(self.report / "m1"),
            ],
            quiet=False,
        )
        m1 = json.loads(result.stdout)
        self.call(
            "frozen-replay",
            [
                sys.executable,
                str(self.share / "tests/m1/replay.py"),
                str(self.share / "tests/m1/results/handoff.json"),
                *common,
                "--report-dir",
                str(self.report / "frozen-replay"),
            ],
            quiet=False,
        )
        self.call(
            "fresh-replay",
            [
                sys.executable,
                str(self.share / "tests/m1/replay.py"),
                str(handoff),
                *common,
                "--report-dir",
                str(self.report / "fresh-replay"),
            ],
            quiet=False,
        )
        self.call(
            "contract-vector",
            [
                str(self.share / "test-tools/bin/sledra-spec-check"),
                "--spec-dir",
                str(self.share / "spec/1.0.0"),
                str(self.share / "spec/fixtures/1.0.0/valid-bundle.json"),
            ],
            quiet=False,
        )
        events = []
        for item in json.loads(handoff.read_text())["files"]:
            if item["path"].startswith(("registry/", "finish/")):
                events.append(json.loads(item["text"])["outbox_event"])
            elif item["path"].startswith("spool/"):
                events.extend(json.loads(line) for line in item["text"].splitlines()[1:])
        for index, event in enumerate(events):
            config = self.work / "event-role.json"
            config.write_text(json.dumps({"producer_role": event["producer"]["role"]}))
            source = self.work / "event.json"
            source.write_text(json.dumps(event))
            self.call(
                f"event-{index}",
                [
                    str(self.share / "test-tools/bin/sledra-event"),
                    "validate",
                    "--config",
                    str(config),
                    "--input",
                    str(source),
                ],
            )
        if len(events) != 19 or m1["summary"]["payload_return_codes"] != [0, 7, 0]:
            raise ValueError("M1 event count or business return codes differ")
        platform_reports = {}
        for name, script, extra in (
            ("shell", "tests/shell/verify.py", []),
            (
                "cross-node",
                "tests/platform/cross_node.py",
                ["simulate", "--samples", "2", "--timeout", "30"],
            ),
            ("primitives", "tests/platform/probe.py", ["--samples", "10"]),
            ("timing", "tests/m1/benchmark.py", ["--samples", "10"]),
        ):
            directory = self.report / name
            self.call(
                name,
                [
                    sys.executable,
                    str(self.share / script),
                    *extra,
                    *common,
                    "--report-dir",
                    str(directory),
                ],
                quiet=False,
            )
            platform_reports[name] = json.loads((directory / "summary.json").read_text())
            if platform_reports[name]["status"] != "pass":
                raise ValueError(f"installed {name} verification failed")
        return {
            "platform_tools": platform_reports,
            "m1": m1,
            "validated_events": len(events),
            "shell_payload_comparison": "pass",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--work-root", type=Path, required=True, help="existing writable test parent"
    )
    parser.add_argument("--report-dir", type=Path, required=True, help="new report directory")
    args = parser.parse_args()
    report = args.report_dir.resolve()
    report.mkdir(parents=True, exist_ok=False)
    package = Path(__file__).resolve().parents[3]
    summary = {
        "ok": False,
        "package": str(package),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "network": "caller-controlled; isolated by make test-package",
    }
    try:
        if sys.version_info < (3, 12):
            raise ValueError("installed verification requires Python >=3.12")
        if not args.work_root.is_dir():
            raise ValueError("work-root must be an existing directory")
        manifest = verify_install(package)
        before = {
            p.relative_to(package).as_posix(): sha(p) for p in package.rglob("*") if p.is_file()
        }
        work = Path(tempfile.mkdtemp(prefix="sledra-installed-", dir=args.work_root.resolve()))
        summary.update(
            work=str(work),
            version=manifest["version"],
            manifest_sha256=sha(package / "manifest.json"),
        )
        acceptance = Acceptance(package, work, report)
        summary.update(acceptance.run(manifest))
        verify_install(package)
        after = {
            p.relative_to(package).as_posix(): sha(p) for p in package.rglob("*") if p.is_file()
        }
        if before != after:
            raise ValueError("installation inventory/bytes changed during verification")
        summary.update(ok=True, installation_unchanged=True)
    except (
        OSError,
        ValueError,
        TypeError,
        KeyError,
        AssertionError,
        subprocess.SubprocessError,
    ) as exc:
        summary["error"] = str(exc)
        print(str(exc), file=sys.stderr)
    save(report / "summary.json", summary)
    print(json.dumps({"ok": summary["ok"], "report": str(report / "summary.json")}, sort_keys=True))
    return int(not summary["ok"])


if __name__ == "__main__":
    raise SystemExit(main())
