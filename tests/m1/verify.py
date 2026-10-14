"""M1 local stage handoff: normal Run, failed/retried Run, and raw evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(REPO))
from scripts.test_session import TestSession, add_arguments  # noqa: E402

RECORDER = REPO / "recorder"
EXAMPLE = REPO / "examples/standalone"
FIXTURES = EXAMPLE / "fixtures"
SPEC = REPO / "spec/1.0.0"
SESSION: TestSession | None = None


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True, indent=2) + "\n")


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def command(binary: Path, env: dict[str, str], *args: str) -> None:
    if SESSION is not None:
        result = SESSION.run([str(binary), *args], env=env, timeout=20)
    else:
        result = subprocess.run([str(binary), *args], env=env, capture_output=True, timeout=20)
    if result.returncode != 0 or result.stdout or result.stderr:
        raise AssertionError(
            f"sledra {args}: rc={result.returncode} stdout={result.stdout!r} stderr={result.stderr!r}"
        )


def payload(mode: str, work: Path) -> tuple[int, bytes, bytes, list[str], bool]:
    work.mkdir(parents=True)
    env = dict(
        SESSION.env if SESSION is not None else os.environ,
        STANDALONE_WORK_DIR=str(work),
        STANDALONE_EXECUTION_LOG=str(work / "executions.log"),
        STANDALONE_PRODUCT=str(work / "product.txt"),
        STANDALONE_PAYLOAD_MODE=mode,
    )
    result = subprocess.run([str(EXAMPLE / "payload.sh")], env=env, capture_output=True, timeout=10)
    return (
        result.returncode,
        result.stdout,
        result.stderr,
        (work / "executions.log").read_text().splitlines(),
        (work / "product.txt").exists(),
    )


def execute_attempt(
    binary: Path,
    env: dict[str, str],
    requests: Path,
    run: Path,
    name: str,
    mode: str,
    retry_of: str | None = None,
) -> tuple[dict, tuple[int, bytes, bytes, list[str], bool]]:
    request = load(FIXTURES / "attempt-request.json")
    request["execution_token"] = f"m1-{name}-execution"
    if retry_of:
        request["retry_of"] = retry_of
    req_path = requests / f"{name}-attempt.json"
    save(req_path, request)
    context = Path(env["SLEDRA_ROOT"]) / "contexts" / f"{name}-attempt.json"
    command(
        binary,
        env,
        "registry",
        "attempt-create",
        "--context",
        str(run),
        "--request-file",
        str(req_path),
        "--context-out",
        str(context),
    )
    command(binary, env, "context", "validate", "--file", str(context))
    attempt = load(context)
    assert attempt["retry_of"] == retry_of
    process_env = dict(env, SLEDRA_CONFIG=str(FIXTURES / "process-config.json"))
    stage_env = dict(env, SLEDRA_CONFIG=str(FIXTURES / "stage-config.json"))
    command(
        binary,
        process_env,
        "event",
        "--context",
        str(context),
        "--type",
        "attempt.started",
        "--invocation-id",
        f"m1-{name}-invocation",
        "--process-role",
        "forecast_program",
        "--strict",
    )
    command(
        binary,
        stage_env,
        "event",
        "--context",
        str(context),
        "--type",
        "stage.started",
        "--payload-file",
        str(FIXTURES / "stage-start.json"),
        "--strict",
    )
    observed = payload(mode, requests / f"{name}-work")
    baseline = payload(mode, requests / f"{name}-baseline")
    assert observed == baseline, f"payload changed for {name}"
    assert observed[3] == ["run"]
    if mode == "success":
        assert observed[0] == 0 and observed[4]
        command(
            binary,
            stage_env,
            "event",
            "--context",
            str(context),
            "--type",
            "stage.ended",
            "--payload-file",
            str(FIXTURES / "stage-end.json"),
            "--strict",
        )
    else:
        assert observed[0] == 7 and not observed[4]
    command(
        binary,
        process_env,
        "event",
        "--context",
        str(context),
        "--type",
        "attempt.exited",
        "--process-role",
        "forecast_program",
        "--exit-code",
        str(observed[0]),
        "--strict",
    )
    return attempt, observed


def create_run(binary: Path, env: dict[str, str], requests: Path, name: str) -> tuple[dict, Path]:
    request = load(FIXTURES / "run-request.json")
    request["launch_token"] = f"m1-{name}-launch"
    request["definition_file"] = str(FIXTURES / "definition.json")
    request["expectation_file"] = str(FIXTURES / "expectation.json")
    req_path = requests / f"{name}-run.json"
    save(req_path, request)
    context = Path(env["SLEDRA_ROOT"]) / "contexts" / f"{name}-run.json"
    command(
        binary,
        env,
        "registry",
        "run-create",
        "--request-file",
        str(req_path),
        "--context-out",
        str(context),
    )
    command(binary, env, "context", "validate", "--file", str(context))
    return load(context), context


def finish(
    binary: Path, env: dict[str, str], requests: Path, run: Path, name: str, attempt_ids: list[str]
) -> bytes:
    request = load(FIXTURES / "finish-request.json")
    request["declaration_id"] = f"m1-{name}-finish"
    request["covered_attempt_ids"] = attempt_ids
    req_path = requests / f"{name}-finish.json"
    save(req_path, request)
    command(binary, env, "run", "finish", "--context", str(run), "--scope-file", str(req_path))
    records = list((Path(env["SLEDRA_ROOT"]) / "finish").rglob("*.json"))
    matches = [p for p in records if load(p)["declaration_id"] == request["declaration_id"]]
    assert len(matches) == 1
    before = matches[0].read_bytes()
    command(binary, env, "run", "finish", "--context", str(run), "--scope-file", str(req_path))
    assert matches[0].read_bytes() == before
    return before


def inspect(root: Path, normal: dict, retry_run: dict, attempts: dict[str, dict]) -> dict:
    events: list[dict] = []
    for path in (root / "spool").rglob("*.jsonl"):
        lines = path.read_bytes().splitlines()
        assert lines and json.loads(lines[0])["record_kind"] == "segment_header"
        assert path.read_bytes().endswith(b"\n")
        events.extend(json.loads(line) for line in lines[1:])
    expected = {
        "normal": ["attempt.started", "stage.started", "stage.ended", "attempt.exited"],
        "failed": ["attempt.started", "stage.started", "attempt.exited"],
        "retry": ["attempt.started", "stage.started", "stage.ended", "attempt.exited"],
    }
    for name, types in expected.items():
        found = [
            e for e in events if e["association"]["attempt_id"] == attempts[name]["attempt_id"]
        ]
        assert sorted(e["event_type"] for e in found) == sorted(types), name
        assert all(e["association"]["run_id"] == attempts[name]["run_id"] for e in found)
    assert len(events) == 11
    assert attempts["failed"]["run_id"] == retry_run["run_id"] == attempts["retry"]["run_id"]
    assert attempts["normal"]["run_id"] == normal["run_id"] != retry_run["run_id"]
    assert attempts["retry"]["retry_of"] == attempts["failed"]["attempt_id"]
    assert attempts["late"]["retry_of"] == attempts["retry"]["attempt_id"]
    assert all(e["association"]["attempt_id"] != attempts["late"]["attempt_id"] for e in events)
    outbox = [load(p)["outbox_event"] for p in (root / "registry").rglob("*.json")]
    outbox += [load(p)["outbox_event"] for p in (root / "finish").rglob("*.json")]
    assert sorted(e["event_type"] for e in outbox) == sorted(
        ["run.registered"] * 2 + ["attempt.registered"] * 4 + ["run.finish_declared"] * 2
    )
    assert len({e["event_id"] for e in outbox}) == len(outbox)
    finish_records = [load(p) for p in (root / "finish").rglob("*.json")]
    retry_finish = next(r for r in finish_records if r["run_id"] == retry_run["run_id"])
    covered = retry_finish["outbox_event"]["payload"]["covered_attempt_ids"]
    assert sorted(covered) == sorted(
        [attempts["failed"]["attempt_id"], attempts["retry"]["attempt_id"]]
    )
    assert attempts["late"]["attempt_id"] not in covered
    return {
        "run_ids": [normal["run_id"], retry_run["run_id"]],
        "attempt_ids": {name: attempt["attempt_id"] for name, attempt in attempts.items()},
        "process_event_types": {name: types for name, types in expected.items()},
        "registry_outbox_types": sorted(e["event_type"] for e in outbox),
        "finish_covered_attempt_ids": covered,
        "source_status": "synthetic; filesystem and node scope available with --report-dir",
        "late_attempt_status": "registered_only; needs_finalization is an M2 projection",
    }


def snapshot(root: Path, summary: dict) -> dict:
    files = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in (".json", ".jsonl"):
            continue
        data = path.read_bytes()
        files.append(
            {"path": str(path.relative_to(root)), "sha256": sha(data), "text": data.decode("utf-8")}
        )
    conflict = REPO / "spec/fixtures/1.0.0/invalid-conflicting-event-id.json"
    return {
        "kind": "m1_handoff_fixture",
        "schema_version": "1.0.0",
        "summary": summary,
        "files": files,
        "counterexamples": {
            "same_event_id_different_content": {
                "path": str(conflict.relative_to(REPO)),
                "sha256": sha(conflict.read_bytes()),
            },
            "partial_open_tail": "See recorder/spool.TestPartialTailAndModes; a missing LF must not advance the M2 checkpoint.",
        },
        "limitations": [
            "synthetic recorder data; platform qualification reported separately",
            "no ingest transaction or projection",
            "open segments have no real ack",
        ],
    }


def main() -> None:
    global SESSION, SPEC
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--handoff-out",
        type=Path,
        help="write a portable synthetic Registry/context/JSONL snapshot",
    )
    add_arguments(parser)
    args = parser.parse_args()
    with TestSession(args, "m1") as session:
        SESSION = session
        SPEC = session.spec
        base = session.work
        root = base / "observability"
        root.mkdir(mode=0o700)
        requests = base / "requests"
        requests.mkdir()
        binary = session.binary
        env = dict(session.env, SLEDRA_ROOT=str(root))
        normal, normal_path = create_run(binary, env, requests, "normal")
        normal_attempt, normal_result = execute_attempt(
            binary, env, requests, normal_path, "normal", "success"
        )
        finish(binary, env, requests, normal_path, "normal", [normal_attempt["attempt_id"]])
        retry_run, retry_path = create_run(binary, env, requests, "retry")
        failed, failed_result = execute_attempt(binary, env, requests, retry_path, "failed", "fail")
        retried, retry_result = execute_attempt(
            binary, env, requests, retry_path, "retry", "success", failed["attempt_id"]
        )
        original_finish = finish(
            binary,
            env,
            requests,
            retry_path,
            "retry",
            [failed["attempt_id"], retried["attempt_id"]],
        )
        late_request = load(FIXTURES / "attempt-request.json")
        late_request.update(execution_token="m1-late-execution", retry_of=retried["attempt_id"])
        late_path = requests / "late-attempt.json"
        save(late_path, late_request)
        late_context = root / "contexts/late-attempt.json"
        command(
            binary,
            env,
            "registry",
            "attempt-create",
            "--context",
            str(retry_path),
            "--request-file",
            str(late_path),
            "--context-out",
            str(late_context),
        )
        command(binary, env, "context", "validate", "--file", str(late_context))
        after_finish = next(
            load(p)
            for p in (root / "finish").rglob("*.json")
            if load(p)["run_id"] == retry_run["run_id"]
        )
        assert (
            json.dumps(after_finish, sort_keys=True).encode()
            == json.dumps(json.loads(original_finish), sort_keys=True).encode()
        )
        summary = inspect(
            root,
            normal,
            retry_run,
            {
                "normal": normal_attempt,
                "failed": failed,
                "retry": retried,
                "late": load(late_context),
            },
        )
        summary["payload_return_codes"] = [normal_result[0], failed_result[0], retry_result[0]]
        bundle = snapshot(root, summary)
        if session.report is not None:
            save(session.report / "handoff.json", bundle)
        session.data.update(
            sample_count=3,
            handoff_files=len(bundle["files"]),
            assertions=summary,
            recorder_root=str(root),
        )
        if args.handoff_out:
            save(args.handoff_out, bundle)
        print(
            json.dumps(
                {
                    "ok": True,
                    "summary": summary,
                    "handoff_files": len(bundle["files"]),
                    "handoff_sha256": sha(
                        (json.dumps(bundle, sort_keys=True, indent=2) + "\n").encode()
                    ),
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
