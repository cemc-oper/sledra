"""Bounded two-role Registry/visibility/flock harness; no scheduler submission."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from scripts.test_session import (  # noqa: E402
    TestSession,
    add_arguments,
    clean_env,
    digest,
    mount_info,
    save,
)
from scripts.package_tools import elf_info, host_arch  # noqa: E402

ROLES = ("a", "b")


def optional_digest(path: Path) -> str | None:
    try:
        return digest(path) if path.is_file() else None
    except OSError:
        return None


def cross_node_identity(mode: str, a: dict, b: dict) -> dict:
    if mode != "hpc" or a["node"] == b["node"]:
        return {"status": "unverified", "reason": "distinct site nodes not confirmed"}
    boot_a, boot_b = a.get("boot_id_sha256"), b.get("boot_id_sha256")
    if boot_a and boot_b:
        if boot_a != boot_b:
            return {
                "status": "pass",
                "reason": "distinct host names and kernel boot identities; scoped to synthetic operations",
                "identity_basis": "boot_id_sha256",
            }
        return {
            "status": "unverified",
            "reason": "host names differ but kernel boot identity is shared",
        }
    machine_a, machine_b = a.get("machine_id"), b.get("machine_id")
    if machine_a and machine_b and machine_a != machine_b:
        return {
            "status": "pass",
            "reason": "distinct host names and machine identities; scoped to synthetic operations",
            "identity_basis": "machine_id",
        }
    return {
        "status": "unverified",
        "reason": "host names differ without distinct boot or machine identities",
    }


def publish(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}-{os.getpid()}.tmp")
    with temporary.open("x") as output:
        json.dump(value, output, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    # All published paths have one writer; replacing a completed role is forbidden.
    try:
        os.link(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        temporary.unlink()


def wait(path: Path, deadline: float, run: Path) -> dict:
    while not path.exists():
        for role in ROLES:
            failed = run / "coord" / f"result-{role}.json"
            if failed.exists() and json.loads(failed.read_text())["status"] == "fail":
                raise RuntimeError(
                    f"peer {role} failed: {json.loads(failed.read_text())['reason']}"
                )
        if time.monotonic() >= deadline:
            raise TimeoutError(f"barrier timed out: {path.name}")
        time.sleep(0.02)
    return json.loads(path.read_text())


def config(run: Path) -> dict:
    if not run.is_absolute() or run.is_symlink():
        raise ValueError("--run-dir must be the same absolute prepared path on every node")
    value = json.loads((run / "cross-config.json").read_text())
    marker = json.loads((run / ".sledra-test-run.json").read_text())
    if value["run_directory"] != str(run) or marker["token"] != value["token"]:
        raise ValueError("prepared path/owner token mismatch")
    return value


def prepare(session: TestSession, samples: int, timeout: float) -> Path:
    run = session.work
    (run / "coord").mkdir()
    (run / "observability").mkdir(mode=0o700)
    (run / "requests").mkdir()
    fixtures = ROOT / "examples/standalone/fixtures"
    request = json.loads((fixtures / "run-request.json").read_text())
    request.update(
        launch_token=f"cross-{session.token}",
        definition_file=str(fixtures / "definition.json"),
        expectation_file=str(fixtures / "expectation.json"),
    )
    save(run / "requests/run.json", request)
    attempt = json.loads((fixtures / "attempt-request.json").read_text())
    attempt["execution_token"] = f"cross-{session.token}"
    save(run / "requests/attempt.json", attempt)
    cfg = {
        "format_version": 1,
        "token": session.token,
        "run_directory": str(run),
        "binary": str(session.binary),
        "binary_sha256": digest(session.binary),
        "spec": str(session.spec),
        "spec_sha256": session.data["spec"],
        "mode": session.args.mode,
        "samples": samples,
        "timeout": timeout,
        "input": session.data["input"],
        "python": platform.python_version(),
        "bash": session.data["bash"],
        "prepare_command": session.data["command"],
        "recorder_root": str(run / "observability"),
        "fixed_context": str(run / "observability/contexts/a-attempt.json"),
    }
    save(run / "cross-config.json", cfg)
    session.data.update(
        sample_count=samples,
        execution={"status": "unverified", "reason": "prepared only"},
        prepared_run=str(run),
        recorder_root=cfg["recorder_root"],
        reason="prepared only; workers and collection not yet run",
    )
    return run


def worker(run: Path, role: str) -> int:
    cfg = config(run)
    coord = run / "coord"
    commands: list[dict] = []
    data = {
        "status": "fail",
        "reason": "preflight incomplete",
        "role": role,
        "node": platform.node(),
        "machine_id": optional_digest(Path("/etc/machine-id")),
        "boot_id_sha256": optional_digest(Path("/proc/sys/kernel/random/boot_id")),
        "architecture": platform.machine(),
        "python": platform.python_version(),
        "command": [sys.executable, *sys.argv],
        "samples": [],
        "run_directory": str(run),
        "token": cfg["token"],
        "mount": mount_info(run),
    }
    deadline = time.monotonic() + cfg["timeout"]
    # Exclusive claim rejects accidental duplicate scheduling of a role.
    publish(coord / f"claim-{role}.json", {"pid": os.getpid(), "node": platform.node()})
    try:
        binary, spec = Path(cfg["binary"]), Path(cfg["spec"])
        if not os.access(binary, os.X_OK) or digest(binary) != cfg["binary_sha256"]:
            raise ValueError("worker binary missing, changed or not executable")
        if elf_info(binary)["arch"] != host_arch():
            raise ValueError("worker binary architecture mismatch; cross-build unverified")
        if {p.name: digest(p) for p in sorted(spec.glob("*.json"))} != cfg["spec_sha256"]:
            raise ValueError("worker spec differs from preparation")
        data["binary"] = {"sha256": digest(binary), "elf": elf_info(binary)}
        env = clean_env() | {
            "SLEDRA_ROOT": cfg["recorder_root"],
            "SLEDRA_SPEC_DIR": str(spec),
            "TMPDIR": str(run),
        }

        def command(*args: str) -> str:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("worker deadline expired")
            argv = [str(binary), *args]
            start = time.monotonic_ns()
            result = subprocess.run(
                argv, env=env, capture_output=True, text=True, timeout=remaining
            )
            commands.append(
                {
                    "argv": argv,
                    "cwd": str(Path.cwd()),
                    "exit_code": result.returncode,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                    "elapsed_ns": time.monotonic_ns() - start,
                }
            )
            if result.returncode:
                raise RuntimeError(f"recorder failed rc={result.returncode}: {result.stderr}")
            return result.stdout

        data["binary"]["version"] = json.loads(command("--version"))
        publish(coord / f"ready-{role}.json", {"ready": True})
        other = "b" if role == "a" else "a"
        wait(coord / f"ready-{other}.json", deadline, run)
        context = run / "observability/contexts" / f"{role}-run.json"
        attempt_context = context.with_name(f"{role}-attempt.json")
        for _ in range(cfg["samples"]):
            start = time.monotonic_ns()
            command(
                "registry",
                "run-create",
                "--request-file",
                str(run / "requests/run.json"),
                "--context-out",
                str(context),
            )
            command(
                "registry",
                "attempt-create",
                "--context",
                str(context),
                "--request-file",
                str(run / "requests/attempt.json"),
                "--context-out",
                str(attempt_context),
            )
            data["samples"].append(time.monotonic_ns() - start)
        publish(
            coord / f"registered-{role}.json",
            {
                "run_id": json.loads(context.read_text())["run_id"],
                "attempt_id": json.loads(attempt_context.read_text())["attempt_id"],
            },
        )
        peer = wait(coord / f"registered-{other}.json", deadline, run)
        own = json.loads((coord / f"registered-{role}.json").read_text())
        if own != peer:
            raise AssertionError("same registration request produced different identities")
        fixed = Path(cfg["fixed_context"])
        command("context", "validate", "--file", str(fixed))
        data["fixed_context"] = {"path": str(fixed), "sha256": digest(fixed)}
        # Both nodes emit into the same shared tree using that identical absolute context.
        if role == "b":
            wait(coord / "event-a.json", deadline, run)
        process_config = json.loads(
            (ROOT / "examples/standalone/fixtures/process-config.json").read_text()
        )
        process_config.update(
            source_id=f"synthetic-cross-{role}", session_id=f"{cfg['token']}-{role}"
        )
        config_path = run / "requests" / f"process-{role}.json"
        save(config_path, process_config)
        env["SLEDRA_CONFIG"] = str(config_path)
        command(
            "event",
            "--context",
            str(fixed),
            "--type",
            "attempt.started",
            "--invocation-id",
            f"cross-{role}",
            "--process-role",
            "forecast_program",
            "--strict",
        )
        publish(coord / f"event-{role}.json", {"visible": True})
        with (coord / "lock").open("a+") as lock:
            if role == "a":
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                publish(coord / "lock-held.json", {"role": role})
                wait(coord / "lock-blocked.json", deadline, run)
                fcntl.flock(lock, fcntl.LOCK_UN)
                publish(coord / "lock-released.json", {"role": role})
                wait(coord / "lock-acquired.json", deadline, run)
            else:
                wait(coord / "lock-held.json", deadline, run)
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    publish(coord / "lock-blocked.json", {"blocked": True})
                else:
                    raise AssertionError("peer lock did not exclude this role")
                wait(coord / "lock-released.json", deadline, run)
                # Nonblocking bounded retry, never hang on a lost peer or filesystem lock.
                while True:
                    try:
                        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError("lock acquisition timed out after release")
                        time.sleep(0.02)
                publish(coord / "lock-acquired.json", {"acquired": True})
                fcntl.flock(lock, fcntl.LOCK_UN)
        ordered = sorted(data["samples"])
        data["registration_pair_ns"] = {
            key: ordered[min(len(ordered) - 1, (len(ordered) * q + 99) // 100 - 1)]
            for key, q in (("p50", 50), ("p95", 95), ("p99", 99), ("max", 100))
        }
        data.update(
            status="pass",
            reason=None,
            assertions={
                "same_registration": True,
                "fixed_path_visible": True,
                "flock_exclusion_and_release": True,
            },
        )
    except (OSError, ValueError, AssertionError, RuntimeError, subprocess.SubprocessError) as exc:
        data["reason"] = str(exc)
    finally:
        data["commands"] = commands
        publish(coord / f"result-{role}.json", data)
    return int(data["status"] != "pass")


def collect(run: Path, report: Path, timeout: float, *, announce: bool = True) -> int:
    report.mkdir(parents=True, exist_ok=False)
    data = {
        "format_version": 1,
        "kind": "cross_node_report",
        "status": "fail",
        "reason": "incomplete",
        "command": [sys.executable, *sys.argv],
        "run_directory": str(run),
        "roles": {},
        "real_cross_node": {"status": "unverified", "reason": "distinct site nodes not confirmed"},
        "qualification": "synthetic operations only; HPC platform, power loss and A6 unverified",
    }
    try:
        cfg = config(run)
        data.update(
            input=cfg["input"],
            binary_sha256=cfg["binary_sha256"],
            spec_sha256=cfg["spec_sha256"],
            mode=cfg["mode"],
            mount=mount_info(run),
            sample_count=cfg["samples"] * 2,
            prepare_command=cfg["prepare_command"],
            token=cfg["token"],
        )
        deadline = time.monotonic() + timeout
        paths = {role: run / "coord" / f"result-{role}.json" for role in ROLES}
        while not all(p.exists() for p in paths.values()):
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "missing worker result: "
                    + ",".join(r for r, p in paths.items() if not p.exists())
                )
            time.sleep(0.02)
        data["roles"] = {role: json.loads(path.read_text()) for role, path in paths.items()}
        for role, result in data["roles"].items():
            if result["status"] != "pass":
                raise AssertionError(f"worker {role} failed: {result['reason']}")
            if result["token"] != cfg["token"] or result["run_directory"] != str(run):
                raise AssertionError("worker result belongs to another run")
            if (
                result["binary"]["sha256"] != cfg["binary_sha256"]
                or len(result["samples"]) != cfg["samples"]
            ):
                raise AssertionError("worker binary/sample mismatch")
        a, b = data["roles"]["a"], data["roles"]["b"]
        if a["fixed_context"] != b["fixed_context"]:
            raise AssertionError("fixed context bytes/path differ across roles")
        if digest(Path(cfg["fixed_context"])) != a["fixed_context"]["sha256"]:
            raise AssertionError("fixed context changed since worker validation")
        registry = list((run / "observability/registry").rglob("*.json"))
        if len(registry) != 2:
            raise AssertionError(f"expected one Run and one Attempt; found {len(registry)} records")
        events = [
            json.loads(line)
            for path in (run / "observability/spool").rglob("*.jsonl")
            for line in path.read_bytes().splitlines()[1:]
        ]
        if len(events) != 2 or {e["payload"]["invocation_id"] for e in events} != {
            "cross-a",
            "cross-b",
        }:
            raise AssertionError("both role spool events were not visible")
        data.update(
            status="pass",
            reason=None,
            registry_records=len(registry),
            process_events=len(events),
            fixed_context=a["fixed_context"],
            work_retained=True,
        )
        data["real_cross_node"] = cross_node_identity(cfg["mode"], a, b)
    except (OSError, ValueError, KeyError, AssertionError, subprocess.SubprocessError) as exc:
        data["reason"] = str(exc)
    finally:
        for role in ROLES:
            path = run / "coord" / f"result-{role}.json"
            if path.exists():
                try:
                    data["roles"][role] = json.loads(path.read_text())
                except (OSError, ValueError) as exc:
                    data["roles"][role] = {"status": "fail", "reason": f"unreadable receipt: {exc}"}
        save(report / "summary.json", data)
        if announce:
            print(f"Report: {report / 'summary.json'}")
    return int(data["status"] != "pass")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("prepare", "simulate"):
        p = commands.add_parser(name)
        add_arguments(p)
        p.add_argument("--samples", type=int, default=8)
        p.add_argument("--timeout", type=float, default=60)
        if name == "simulate":
            p.add_argument(
                "--omit-role", choices=ROLES, help="simulate missing node; expected failure"
            )
    p = commands.add_parser("worker")
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--role", choices=ROLES, required=True)
    p = commands.add_parser("collect")
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--report-dir", type=Path, required=True)
    p.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args()
    if args.action == "worker":
        return worker(args.run_dir, args.role)
    if not 0.1 <= args.timeout <= 600:
        parser.error("timeout must be 0.1..600 seconds")
    if args.action == "collect":
        return collect(args.run_dir, args.report_dir, args.timeout)
    if not 1 <= args.samples <= 100:
        parser.error("samples must be 1..100")
    if args.action == "prepare":
        args.keep_work = True
    with TestSession(args, "cross_node") as session:
        run = prepare(session, args.samples, args.timeout)
        print(json.dumps({"run_directory": str(run), "roles": list(ROLES)}))
        if args.action == "simulate":
            processes = []
            intermediate = session.report or session.work
            worker_output = {}
            try:
                for role in ROLES:
                    if role != args.omit_role:
                        log = (
                            (session.report / f"worker-{role}.log").open("w")
                            if session.report is not None
                            else None
                        )
                        child = subprocess.Popen(
                            [
                                sys.executable,
                                str(Path(__file__).resolve()),
                                "worker",
                                "--run-dir",
                                str(run),
                                "--role",
                                role,
                            ],
                            env=clean_env(),
                            stdout=log if log is not None else subprocess.PIPE,
                            stderr=subprocess.STDOUT,
                        )
                        processes.append((role, child, log))
                codes = []
                for role, child, log in processes:
                    if log is None:
                        output, _ = child.communicate(timeout=args.timeout + 5)
                        worker_output[role] = output.decode(errors="replace")
                        codes.append(child.returncode)
                    else:
                        codes.append(child.wait(timeout=args.timeout + 5))
                code = collect(
                    run, intermediate / "collection", 0.2, announce=session.report is not None
                )
                session.data["execution"] = {
                    "status": "fail" if code or any(codes) else "pass",
                    "reason": "same-host harness only",
                }
                session.data["collection"] = json.loads(
                    (intermediate / "collection/summary.json").read_text()
                )
                if code or any(codes):
                    print(session.data["collection"]["reason"], file=sys.stderr)
                    for role, _, log in processes:
                        if log is not None:
                            log.flush()
                        content = Path(log.name).read_text() if log else worker_output.get(role, "")
                        if content:
                            print(content, file=sys.stderr)
                    raise AssertionError(
                        "cross-node simulation failed; see collection and worker logs"
                    )
                print(json.dumps({"ok": True, "collection": "pass"}, sort_keys=True))
            finally:
                for _, child, log in processes:
                    if child.poll() is None:
                        child.kill()
                        child.wait(timeout=5)
                    if log is not None:
                        log.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
