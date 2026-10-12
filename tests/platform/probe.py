"""Bounded single-node SLD-03 primitives; local/HPC roots with separate qualification."""

from __future__ import annotations

import argparse
import concurrent.futures
import fcntl
import json
import os
import signal
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path


HERE = Path(__file__).resolve().parent
PROJECT = HERE.parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(PROJECT))
from scripts.test_session import TestSession, add_arguments  # noqa: E402

PAYLOAD = b"synthetic-sld-03-record\n"


def file_write_sync(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def link_contender(args: tuple[str, str, bytes]) -> bool:
    folder, target, data = args
    temporary = Path(folder) / ("candidate-" + uuid.uuid4().hex)
    file_write_sync(temporary, data)
    try:
        try:
            os.link(temporary, target)
            return True
        except FileExistsError:
            return False
    finally:
        temporary.unlink()


def link_and_crash(args: list[str]) -> None:
    folder, target, phase = args
    temporary = Path(folder) / ("crash-" + uuid.uuid4().hex)
    file_write_sync(temporary, PAYLOAD)
    if phase == "before_link":
        os._exit(97)
    os.link(temporary, target)
    os._exit(98)


def timed(fn) -> int:
    start = time.monotonic_ns()
    fn()
    return time.monotonic_ns() - start


def percentiles(values: list[int]) -> dict[str, int]:
    ordered = sorted(values)
    return {
        label: ordered[min(len(ordered) - 1, (len(ordered) * quantile + 99) // 100 - 1)]
        for label, quantile in (("p50_ns", 50), ("p95_ns", 95), ("p99_ns", 99), ("max_ns", 100))
    }


def test_filesystem(root: Path, samples: int) -> dict:
    publish = root / "publish"
    publish.mkdir()
    source = publish / "source"
    target = publish / "target"
    file_write_sync(source, PAYLOAD)
    os.link(source, target)
    sync_directory(publish)
    assert source.stat().st_ino == target.stat().st_ino
    assert target.read_bytes() == PAYLOAD
    overwrite_rejected = False
    try:
        os.link(source, target)
    except FileExistsError:
        overwrite_rejected = True
    assert overwrite_rejected

    race = root / "race"
    race.mkdir()
    with concurrent.futures.ProcessPoolExecutor(max_workers=16) as pool:
        same = list(pool.map(link_contender, [(str(race), str(race / "same"), PAYLOAD)] * 100))
        distinct = list(
            pool.map(
                link_contender,
                [(str(race), str(race / f"distinct-{i}"), PAYLOAD) for i in range(100)],
            )
        )
    sync_directory(race)
    assert sum(same) == 1 and sum(distinct) == 100
    assert (race / "same").read_bytes() == PAYLOAD

    crash = root / "crash"
    crash.mkdir()
    pre = crash / "before"
    post = crash / "after"
    for phase, path, code in (("before_link", pre, 97), ("after_link", post, 98)):
        cmd = [
            sys.executable,
            str(HERE / "probe.py"),
            "--crash-child",
            str(crash),
            str(path),
            phase,
        ]
        assert subprocess.run(cmd, check=False, timeout=10).returncode == code
    assert not pre.exists() and post.read_bytes() == PAYLOAD
    retry = crash / "retry"
    file_write_sync(retry, PAYLOAD)
    os.link(retry, pre)
    assert pre.read_bytes() == PAYLOAD
    try:
        os.link(retry, post)
        raise AssertionError("retry replaced the winning record")
    except FileExistsError:
        assert post.read_bytes() == PAYLOAD
    sync_directory(crash)

    lock_path = root / "lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    child_code = "import fcntl,os,sys; f=os.open(sys.argv[1],os.O_RDWR); fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)"
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        blocked = subprocess.run(
            [sys.executable, "-c", child_code, str(lock_path)], capture_output=True, timeout=10
        )
        assert blocked.returncode != 0 and b"BlockingIOError" in blocked.stderr
        fcntl.flock(fd, fcntl.LOCK_UN)
        acquired = subprocess.run(
            [sys.executable, "-c", child_code, str(lock_path)], capture_output=True, timeout=10
        )
        assert acquired.returncode == 0, acquired.stderr
    finally:
        os.close(fd)

    fixed = root / "fixed-context.json"
    file_write_sync(fixed, b'{"synthetic":true}\n')
    reader = subprocess.run(
        [
            sys.executable,
            "-c",
            "import pathlib,sys;print(pathlib.Path(sys.argv[1]).read_text(),end='')",
            str(fixed.resolve()),
        ],
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert reader.stdout == fixed.read_bytes()

    latency: dict[str, list[int]] = {
        key: [] for key in ("create_fsync", "link_dir_fsync", "stat_read")
    }
    payload = bytes(4096)
    for i in range(samples):
        temporary = root / f"sample-{i}.tmp"
        destination = root / f"sample-{i}.json"
        latency["create_fsync"].append(timed(lambda: file_write_sync(temporary, payload)))
        latency["link_dir_fsync"].append(
            timed(lambda: (os.link(temporary, destination), sync_directory(root)))
        )
        latency["stat_read"].append(timed(lambda: (destination.stat(), destination.read_bytes())))
        temporary.unlink()
        destination.unlink()
    sync_directory(root)
    return {
        "status": "pass",
        "filesystem_type": subprocess.check_output(
            ["findmnt", "-T", str(root), "-o", "FSTYPE", "-n"], text=True
        ).strip(),
        "hardlink_no_replace": True,
        "same_request_100_winners": sum(same),
        "distinct_request_100_winners": sum(distinct),
        "directory_fsync": True,
        "process_crash_before_link_absent": True,
        "process_crash_after_link_visible": True,
        "cross_process_flock": True,
        "fixed_absolute_path_second_process": True,
        "latency_ns": {key: percentiles(values) for key, values in latency.items()},
        "latency_raw_ns": latency,
        "sample_count": samples,
        "sample_bytes": len(payload),
    }


def test_shell() -> dict:
    script = HERE / "shell_probe.sh"
    baseline_times = []
    for _ in range(20):
        start = time.monotonic_ns()
        normal = subprocess.run(
            ["bash", str(script), "baseline"], capture_output=True, timeout=10, check=True
        )
        baseline_times.append(time.monotonic_ns() - start)
        assert normal.stdout == b"start\nend\n" and normal.stderr == b"exit:0\n"
    results = {}
    for name in ("INT", "TERM", "HUP"):
        proc = subprocess.Popen(
            ["bash", str(script), "signal"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            assert proc.stdout.readline() == b"ready\n"
            os.killpg(proc.pid, getattr(signal, "SIG" + name))
            proc.wait(timeout=5)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            stdout, stderr = proc.communicate(timeout=5)
            expected = 128 + getattr(signal, "SIG" + name).value
            assert proc.returncode == expected, (name, proc.returncode, stderr)
            assert stdout == b"" and f"signal:{name}\n".encode() in stderr
            results[name] = {"exit_code": proc.returncode, "trap": True}
        finally:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            if proc.poll() is None:
                proc.wait(timeout=5)
    return {
        "status": "pass",
        "baseline_stdout": normal.stdout.decode(),
        "baseline_exit": normal.returncode,
        "no_observability_shell_baseline_ns": percentiles(baseline_times),
        "baseline_runs": len(baseline_times),
        "set_euo_pipefail_background_wait": True,
        "signals": results,
    }


def test_static_binary(session: TestSession) -> dict:
    run_times = []
    for _ in range(12):
        start = time.monotonic_ns()
        result = session.run([str(session.binary), "--version"], timeout=10)
        run_times.append(time.monotonic_ns() - start)
        assert (
            result.returncode == 0
            and json.loads(result.stdout) == session.data["binary"]["version"]
        )
    return {
        "status": "pass",
        "binary": session.data["binary"],
        "static": session.data["binary"]["elf"]["static"],
        "recorder_version_startup_ns": percentiles(run_times),
        "raw_ns": run_times,
        "runs": len(run_times),
    }


def temporary_tree_usage(root: Path) -> dict:
    entries = list(root.rglob("*"))
    files = [path for path in entries if path.is_file()]
    logical_bytes = sum(path.stat().st_size for path in files)
    assert len(files) <= 200 and logical_bytes <= 10 * 1024 * 1024
    return {
        "files": len(files),
        "directories": sum(path.is_dir() for path in entries),
        "logical_file_bytes": logical_bytes,
        "test_cap_files": 200,
        "test_cap_logical_bytes": 10 * 1024 * 1024,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    parser.add_argument("--parent", type=Path, help="legacy alias for --work-root")
    parser.add_argument("--samples", type=int, default=64)
    parser.add_argument("--crash-child", nargs=3, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.crash_child:
        link_and_crash(args.crash_child)
    if not 10 <= args.samples <= 1000:
        parser.error("--samples must be between 10 and 1000")
    if args.parent:
        if args.work_root:
            parser.error("choose --parent or --work-root")
        args.work_root = args.parent
    with TestSession(args, "platform") as session:
        if shutil.which("findmnt") is None:
            raise ValueError("missing platform probe dependency: findmnt")
        report = {
            "probe_version": "2",
            "kind": "synthetic_single_node_platform_probe",
            "filesystem": test_filesystem(session.work, args.samples),
            "shell": test_shell(),
            "static_binary": test_static_binary(session),
            "temporary_tree": temporary_tree_usage(session.work),
        }
        session.data.update(sample_count=args.samples, results=report)
        print(json.dumps(report, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
