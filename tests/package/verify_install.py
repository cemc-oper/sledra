"""Verify an unpacked trial using Python's standard library and bundled tools."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import tarfile
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

    def child_events(self, manifest: dict) -> dict:
        capability = self.call(
            "child-events-capability",
            [
                str(self.package / "bin/sledra"),
                "execution",
                "child-events",
                "capabilities",
                "--interface",
                "CE-1",
                "--protocol",
                "1",
            ],
            quiet=False,
        )
        if capability.stdout != b"CE-1\texecution-child-events\t1\n":
            raise ValueError("installed binary does not support the advertised CE-1 protocol")
        if manifest.get("child_events", {}).get("interface") != "CE-1":
            raise ValueError("package does not advertise child-events resources")
        library = self.share / "integrations/bash"
        example = self.share / "examples/execution-child-events"
        expected = b"".join(f"synthetic ready:{lead:03d}\n".encode() for lead in (0, 3, 6, 9, 12))
        facts = {}
        for case, mode, count in (
            ("inline", "inline", 8),
            ("external", "external", 8),
            ("off", "external", 0),
            ("missing-owner", "external", 0),
            ("missing-events", "external", 3),
            ("missing-sibling", "external", 0),
            ("protocol-mismatch", "external", 0),
            ("unattached", "external", 0),
        ):
            work = self.work / ("child-events-" + case)
            runtime = work / "runtime"
            runtime.mkdir(parents=True, mode=0o700)
            records = work / "records"
            self.call(
                "init-" + case,
                [
                    str(self.package / "bin/sledra"),
                    "execution",
                    "init-root",
                    "--root",
                    str(records),
                    "--system",
                    "synthetic",
                    "--deployment",
                    "installed",
                    "--partition-timezone=+00:00",
                ],
            )
            env = dict(
                self.env,
                SLEDRA_MODE="execution-v2",
                SLEDRA_BIN=str(self.package / "bin/sledra"),
                SLEDRA_ROOT=str(records),
                SLEDRA_RUNTIME_DIR=str(runtime),
                SLEDRA_BASH_LIBRARY=str(library / "sledra-execution.sh"),
                SLEDRA_EVENTS_LIBRARY=str(library / "sledra-events.sh"),
            )
            if case == "off":
                env["SLEDRA_MODE"] = "off"
            elif case == "missing-owner":
                env["SLEDRA_BASH_LIBRARY"] = str(work / "absent-owner.sh")
            elif case == "missing-events":
                env["SLEDRA_EVENTS_LIBRARY"] = str(work / "absent-events.sh")
            elif case == "missing-sibling":
                detached = work / "owner-only.sh"
                shutil.copyfile(library / "sledra-execution.sh", detached)
                env["SLEDRA_BASH_LIBRARY"] = str(detached)
            elif case == "protocol-mismatch":
                proxy = work / "incompatible-recorder"
                proxy.write_text(
                    '#!/usr/bin/env bash\nprintf "CE-1\\texecution-child-events\\t0\\n"\n'
                )
                proxy.chmod(0o700)
                env["SLEDRA_BIN"] = str(proxy)
            args = ["bash", str(example / "owner.sh"), mode]
            if case == "unattached":
                args = ["bash", str(example / "business.sh")]
            result = self.call("child-events-" + case, args, env=env, quiet=False)
            diagnostic = b'{"ok":false,"code":"execution_observation_unavailable"}\n'
            stderr = diagnostic if case in {"missing-sibling", "protocol-mismatch"} else b""
            if (result.stdout, result.stderr) != (expected, stderr):
                raise ValueError(f"{case} changed business output or bounded diagnostic")
            streams = sorted(records.glob("executions/*/*/*.jsonl"))
            events = [
                json.loads(line) for path in streams for line in path.read_bytes().splitlines()
            ]
            if len(events) != count or len(streams) != int(count > 0):
                raise ValueError(f"{case} produced unexpected lifecycle/facts")
            if count:
                if [event["name"] for event in events] != [
                    "execution.started",
                    "execution.context",
                    *(["data.ready"] * 5 if count == 8 else []),
                    "execution.ended",
                ]:
                    raise ValueError(f"{case} lifecycle/order changed")
                if count == 8 and [e["attributes"]["lead"] for e in events[2:-1]] != [
                    0,
                    3,
                    6,
                    9,
                    12,
                ]:
                    raise ValueError("installed child facts are missing or out of order")
            before = {
                str(p): sha(p)
                for base in (records, runtime)
                for p in base.rglob("*")
                if p.is_file()
            }
            if any(
                p.name in {"writer.lock", "state.json", "owner.json"} for p in records.rglob("*")
            ):
                raise ValueError("control files leaked into recording root")
            business = work / "business"
            business.mkdir()
            (business / "product").write_text("synthetic business-only cleanup fixture\n")
            with tarfile.open(work / "records.tar", "w") as archive:
                archive.add(records, arcname="records")
            with tarfile.open(work / "records.tar") as archive:
                if any(
                    not name.startswith("records") or "runtime" in Path(name).parts
                    for name in archive.getnames()
                ):
                    raise ValueError("record archive includes control directory")
            shutil.rmtree(business)
            after = {
                str(p): sha(p)
                for base in (records, runtime)
                for p in base.rglob("*")
                if p.is_file()
            }
            if before != after:
                raise ValueError("business cleanup changed recording/control files")
            if any(p.stat().st_mode & 0o077 for p in runtime.rglob("*")):
                raise ValueError("private control resources have public permissions")
            facts[case] = {
                "events": len(events),
                "streams": len(streams),
                "business_exit": result.returncode,
                "control_separate": True,
                "archive_and_cleanup": "fixture passed",
            }
        return facts

    def run(self, manifest: dict) -> dict:
        if not (self.share / "integrations/bash/sledra.sh").is_file():
            raise ValueError("installed Bash integration is missing")
        if (self.share / "shell").exists():
            raise ValueError("legacy Bash integration path must not be packaged")
        result = self.call("version", [str(self.package / "bin/sledra"), "--version"], quiet=False)
        if json.loads(result.stdout) != manifest["version"]:
            raise ValueError("binary version differs from package manifest")
        self.call("help", [str(self.package / "bin/sledra"), "--help"], quiet=False)
        self.shell_examples()
        child_events = self.child_events(manifest)
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
            "child_events": child_events,
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
