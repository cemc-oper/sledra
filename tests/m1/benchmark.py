"""Bounded local M1 recorder timing probe; never qualifies the target HPC FS."""

from __future__ import annotations

import argparse
import json
import math
import platform
import subprocess
import time
from pathlib import Path

from verify import EXAMPLE, FIXTURES, REPO, SPEC, command, load, save, sha
from scripts.test_session import TestSession, add_arguments, clean_env
import verify


def quantiles(samples: list[int]) -> dict[str, int]:
    ordered = sorted(samples)
    assert ordered
    return {
        name: ordered[min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1)]
        for name, q in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99), ("max", 1.0))
    }


def timed(binary: Path, env: dict[str, str], *args: str) -> int:
    start = time.perf_counter_ns()
    command(binary, env, *args)
    return time.perf_counter_ns() - start


def shell_case(binary: Path, base: Path, enabled: bool) -> tuple[int, tuple]:
    base.mkdir()
    root = base / "observability"
    root.mkdir(mode=0o700)
    env = dict(
        clean_env(),
        STANDALONE_WORK_DIR=str(base),
        SLEDRA_ROOT=str(root),
        SLEDRA_SPEC_DIR=str(SPEC),
        SLEDRA_CONFIG=str(FIXTURES / "process-config.json"),
        SLEDRA_BIN=str(binary),
        SLEDRA_ENABLED="1" if enabled else "0",
    )
    start = time.perf_counter_ns()
    result = subprocess.run([str(EXAMPLE / "run.sh")], env=env, capture_output=True, timeout=20)
    elapsed = time.perf_counter_ns() - start
    execution = (base / "executions.log").read_bytes()
    callbacks = (base / "callbacks.log").read_bytes()
    product = (base / "product.txt").read_bytes()
    assert result.returncode == 0 and execution == b"run\n"
    return elapsed, (result.returncode, result.stdout, result.stderr, execution, callbacks, product)


def measure(binary: Path, base: Path, samples: int) -> dict:
    enabled_times, disabled_times, added = [], [], []
    for i in range(samples):
        first_enabled = i % 2 == 1
        cases = {}
        for enabled in (first_enabled, not first_enabled):
            cases[enabled] = shell_case(
                binary, base / f"shell-{i}-{'on' if enabled else 'off'}", enabled
            )
        assert cases[True][1] == cases[False][1], f"business mismatch at sample {i}"
        disabled_times.append(cases[False][0])
        enabled_times.append(cases[True][0])
        added.append(cases[True][0] - cases[False][0])

    root = base / "cli-root"
    root.mkdir(mode=0o700)
    requests = base / "cli-requests"
    requests.mkdir()
    env = dict(clean_env(), SLEDRA_ROOT=str(root), SLEDRA_SPEC_DIR=str(SPEC))
    run_times, attempt_times = [], []
    for i in range(samples):
        req = load(FIXTURES / "run-request.json")
        req["launch_token"] = f"m1-bench-run-{i}"
        req["definition_file"] = str(FIXTURES / "definition.json")
        req["expectation_file"] = str(FIXTURES / "expectation.json")
        req_path = requests / f"run-{i}.json"
        save(req_path, req)
        context = root / "contexts" / f"run-{i}.json"
        run_times.append(
            timed(
                binary,
                env,
                "registry",
                "run-create",
                "--request-file",
                str(req_path),
                "--context-out",
                str(context),
            )
        )
        attempt = load(FIXTURES / "attempt-request.json")
        attempt["execution_token"] = f"m1-bench-attempt-{i}"
        attempt_path = requests / f"attempt-{i}.json"
        save(attempt_path, attempt)
        attempt_context = root / "contexts" / f"attempt-{i}.json"
        attempt_times.append(
            timed(
                binary,
                env,
                "registry",
                "attempt-create",
                "--context",
                str(context),
                "--request-file",
                str(attempt_path),
                "--context-out",
                str(attempt_context),
            )
        )

    event_env = dict(env, SLEDRA_CONFIG=str(FIXTURES / "stage-config.json"))
    event_context = root / "contexts/attempt-0.json"
    emit_times = []
    for i in range(samples):
        payload = requests / f"stage-{i}.json"
        save(payload, {"interval_id": f"m1-bench-{i}", "stage_key": "forecast"})
        emit_times.append(
            timed(
                binary,
                event_env,
                "event",
                "--context",
                str(event_context),
                "--type",
                "stage.started",
                "--payload-file",
                str(payload),
                "--strict",
            )
        )
    return {
        "samples": samples,
        "unit": "nanoseconds",
        "shell_disabled": {"raw": disabled_times, "summary": quantiles(disabled_times)},
        "shell_enabled": {"raw": enabled_times, "summary": quantiles(enabled_times)},
        "paired_added": {"raw": added, "summary": quantiles(added)},
        "run_create": {"raw": run_times, "summary": quantiles(run_times)},
        "attempt_create": {"raw": attempt_times, "summary": quantiles(attempt_times)},
        "event_emit": {"raw": emit_times, "summary": quantiles(emit_times)},
        "event_first_ns": emit_times[0],
        "event_subsequent": {"raw": emit_times[1:], "summary": quantiles(emit_times[1:])},
    }


def main() -> None:
    global SPEC
    parser = argparse.ArgumentParser()
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--out", type=Path)
    add_arguments(parser)
    args = parser.parse_args()
    if not 10 <= args.samples <= 100:
        parser.error("samples must be 10..100")
    with TestSession(args, "benchmark") as session:
        SPEC = session.spec
        verify.SESSION = session
        base = session.work
        binary = session.binary
        data = measure(binary, base, args.samples)
        data.update(
            {
                "kind": "m1_local_recorder_timing",
                "platform": platform.platform(),
                "architecture": platform.machine(),
                "filesystem": session.data["mount"]["filesystem"],
                "go_version": session.data["binary"]["version"]["go_version"],
                "python_version": platform.python_version(),
                "binary_sha256": sha(binary.read_bytes()),
                "input_sha256": {
                    str(path.relative_to(REPO)): sha(path.read_bytes())
                    for path in (
                        EXAMPLE / "run.sh",
                        EXAMPLE / "payload.sh",
                        FIXTURES / "run-request.json",
                        FIXTURES / "attempt-request.json",
                        FIXTURES / "process-config.json",
                        FIXTURES / "stage-config.json",
                        REPO / "tests/m1/benchmark.py",
                    )
                },
                "qualification": "local synthetic measurement only; A6 thresholds and target shared/HPC filesystem unverified",
            }
        )
        session.data.update(sample_count=args.samples, measurement=data)
        save(session.report / "samples.json", data)
        if args.out:
            save(args.out, data)
        print(
            json.dumps(
                {
                    key: data[key]["summary"]
                    for key in (
                        "shell_disabled",
                        "shell_enabled",
                        "paired_added",
                        "run_create",
                        "attempt_create",
                        "event_emit",
                    )
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
