"""Exercise generated jobs, mock submission boundaries, and offline replay."""

from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from replay import replay
from runner import Harness


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def business(
    harness: Harness,
    launch: str,
    *,
    event_bin: Path | None = None,
    context_override: Path | None = None,
) -> list[dict]:
    harness.launch(launch, "2026-10-16T00:00:00Z")
    return [
        harness.attempt(
            launch, task, task + "-one", event_bin=event_bin, context_override=context_override
        )
        for task in ("prepare", "forecast", "verify")
    ]


def check_equivalence(bundle: Path, workspace: Path, sledra_bin: Path) -> dict:
    cases = {
        "disabled": Harness(bundle, workspace / "disabled", sledra_bin, enabled=False),
        "enabled": Harness(bundle, workspace / "enabled", sledra_bin, enabled=True),
        "no-recorder": Harness(
            bundle, workspace / "no-recorder", workspace / "missing-recorder", enabled=True
        ),
        "attempt-registration-failure": Harness(
            bundle, workspace / "attempt-registration-failure", sledra_bin, enabled=True
        ),
        "no-shell-api": Harness(
            bundle,
            workspace / "no-shell-api",
            sledra_bin,
            enabled=True,
            shell_api=workspace / "missing-shell-api",
        ),
        "bad-context": Harness(bundle, workspace / "bad-context", sledra_bin, enabled=True),
        "event-failure": Harness(bundle, workspace / "event-failure", sledra_bin, enabled=True),
    }
    fail_bin = workspace / "fail-event.sh"
    fail_bin.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    fail_bin.chmod(0o755)
    results = {}
    for label, harness in cases.items():
        extra = {}
        if label == "attempt-registration-failure":
            harness.launch("comparison", "2026-10-16T00:00:00Z")
            harness.sledra_bin = workspace / "missing-recorder"
        if label == "bad-context":
            extra["context_override"] = workspace / "absent-context.json"
        if label == "event-failure":
            extra["event_bin"] = fail_bin
        results[label] = business(harness, "comparison", **extra)
        invocation = harness.invocation("comparison")
        assert lines(invocation / "executions.log") == [
            "prepare:prepare-one",
            "forecast:forecast-one",
            "verify:verify-one",
        ]
        assert lines(invocation / "callbacks.log") == [
            "prepare:prepare-one:0",
            "forecast:forecast-one:0",
            "verify:verify-one:0",
        ]
        assert (
            len(
                [
                    load_line
                    for load_line in lines(invocation / "mock-submissions.jsonl")
                    if json.loads(load_line)["phase"] == "submit"
                ]
            )
            == 3
        )
        assert all(result["payload_rc"] == 0 for result in results[label])
    baseline = [
        (item["task"], item["payload_rc"], item["stdout"], item["stderr"])
        for item in results["disabled"]
    ]
    for label, items in results.items():
        assert [
            (item["task"], item["payload_rc"], item["stdout"], item["stderr"]) for item in items
        ] == baseline, label
        assert [
            json.loads(line)["argv"]
            for line in lines(cases[label].invocation("comparison") / "mock-submissions.jsonl")
            if json.loads(line)["phase"] == "submit"
        ] == [[str(bundle / "jobs" / f"{task}.sh")] for task in ("prepare", "forecast", "verify")]
    assert results["no-recorder"][0]["attempt_id"] is None
    assert results["attempt-registration-failure"][0]["attempt_id"] is None
    assert all(item["attempt_id"] for item in results["enabled"])
    assert not (cases["disabled"].root / "spool").exists()
    return {"cases": sorted(results), "business": baseline}


def check_identities(bundle: Path, workspace: Path, sledra_bin: Path) -> dict:
    harness = Harness(bundle, workspace / "identities", sledra_bin, enabled=True)
    cycle_zero = "2026-10-16T00:00:00Z"
    cycle_six = "2026-10-16T06:00:00Z"
    harness.launch("first", cycle_zero)
    original_context = harness.context("first").read_bytes()
    harness.launch("first", cycle_zero)
    assert harness.context("first").read_bytes() == original_context
    with ThreadPoolExecutor(max_workers=2) as pool:
        future_b = pool.submit(harness.launch, "second", cycle_six)
        future_c = pool.submit(harness.launch, "third", cycle_zero)
        future_b.result()
        future_c.result()
    harness.launch("missing-product", cycle_zero)
    runs = {
        name: load(harness.context(name))
        for name in ("first", "second", "third", "missing-product")
    }
    assert len({run["run_id"] for run in runs.values()}) == 4
    assert len({run["definition_ref"]["id"] for run in runs.values()}) == 1
    assert runs["first"]["cycle"] == runs["third"]["cycle"]
    assert runs["first"]["cycle"] != runs["second"]["cycle"]

    with ThreadPoolExecutor(max_workers=3) as pool:
        first_prepare = pool.submit(harness.attempt, "first", "prepare", "p1")
        second_prepare = pool.submit(harness.attempt, "second", "prepare", "p1")
        third_prepare = pool.submit(
            harness.attempt, "third", "prepare", "early", mode="start-before-return"
        )
        p1 = first_prepare.result()
        second_prepare.result()
        early = third_prepare.result()
    for name in ("first", "second", "third"):
        invocation = harness.invocation(name)
        recorded_paths = lines(invocation / "context-paths.log")
        assert len(recorded_paths) == 1
        assert Path(recorded_paths[0]).is_file()
        assert load(Path(recorded_paths[0]))["run_id"] == runs[name]["run_id"]
    f1 = harness.attempt("first", "forecast", "f1", fail_forecast=True)
    f2 = harness.attempt("first", "forecast", "f2", retry_of=f1["attempt_id"])
    v1 = harness.attempt("first", "verify", "v1")
    assert (p1["payload_rc"], f1["payload_rc"], f2["payload_rc"], v1["payload_rc"]) == (0, 7, 0, 0)
    assert f1["stderr"] == "forecast failed\n"
    assert f1["attempt_id"] != f2["attempt_id"]
    assert load(Path(f2["context"]))["retry_of"] == f1["attempt_id"]
    before = lines(harness.invocation("first") / "executions.log")
    assert harness.attempt("first", "forecast", "f2", retry_of=f1["attempt_id"]) == f2
    assert lines(harness.invocation("first") / "executions.log") == before
    assert harness.finish(
        "first", "first-finish", [item["attempt_id"] for item in (p1, f1, f2, v1)]
    )
    late = harness.attempt("first", "verify", "v2", retry_of=v1["attempt_id"])
    assert load(Path(late["context"]))["run_id"] == runs["first"]["run_id"]

    failed = harness.attempt("second", "forecast", "f1", fail_forecast=True)
    branch_a = harness.attempt("second", "forecast", "f2", retry_of=failed["attempt_id"])
    branch_b = harness.attempt("second", "forecast", "f3", retry_of=failed["attempt_id"])
    assert branch_a["attempt_id"] != branch_b["attempt_id"]

    rejected = harness.attempt("third", "prepare", "reject", mode="reject")
    timeout = harness.attempt("third", "prepare", "timeout", mode="timeout")
    lost = harness.attempt("third", "prepare", "lost", mode="accept-record-failure")
    assert rejected["attempt_id"] and rejected["job_id"] is None
    assert rejected["payload_rc"] is None and timeout["submission_outcome"] == "unknown"
    assert timeout["payload_rc"] is None
    assert lost["submission_outcome"] == "unknown" and lost["payload_rc"] == 0
    assert early["job_id"] and early["payload_rc"] == 0
    assert early["job_ref_id"]
    submissions = [
        json.loads(line) for line in lines(harness.invocation("third") / "mock-submissions.jsonl")
    ]
    assert len([row for row in submissions if row["phase"] == "submit"]) == 4
    assert len(lines(harness.invocation("third") / "executions.log")) == 2
    phases = [row["phase"] for row in submissions if row["token"] == "early"]
    assert phases == ["submit", "payload-returned", "accepted"]

    absent = harness.attempt("missing-product", "verify", "v1")
    assert absent["payload_rc"] == 3
    assert (
        load(harness.invocation("missing-product") / "attempts/verify-v1/validation.json")["result"]
        == "fail"
    )

    data = replay(harness.workspace, workspace / "facts.sqlite")
    by_run = {run["id"]: run for run in data["runs"]}
    first = by_run[runs["first"]["run_id"]]
    assert first["lifecycle"]["state"] == "needs_finalization"
    assert first["finish_declarations"][0]["declaration_id"] == "first-finish"
    assert late["attempt_id"] in first["attempt_ids"]
    instances = {item["id"]: item for item in data["instances"]}
    first_forecast = instances[load(Path(f1["context"]))["task_instance_id"]]
    assert first_forecast["execution"]["state"] == "recovered"
    second_forecast = instances[load(Path(failed["context"]))["task_instance_id"]]
    assert second_forecast["execution"]["state"] == "ambiguous"
    attempts = {item["id"]: item for item in data["attempts"]}
    assert attempts[f1["attempt_id"]]["execution"]["exit_code"] == 7
    assert attempts[f2["attempt_id"]]["execution"]["exit_code"] == 0
    assert attempts[rejected["attempt_id"]]["job_ref_ids"] == []
    assert attempts[timeout["attempt_id"]]["job_ref_ids"] == []
    assert attempts[lost["attempt_id"]]["job_ref_ids"] == []
    assert attempts[early["attempt_id"]]["execution"]["state"] == "exited"
    assert attempts[early["attempt_id"]]["job_ref_ids"] == [early["job_ref_id"]]
    types = [fact["event_type"] for fact in data["facts"]]
    assert "stage.started" in types and "stage.ended" in types
    assert "delivery.confirmed" not in types
    assert not data["document_rejections"] and not data["rejected_facts"]
    assert all(source["quarantine_count"] == 0 for source in data["source_health"])
    return {
        "definition_id": runs["first"]["definition_ref"]["id"],
        "run_ids": {name: run["run_id"] for name, run in runs.items()},
        "replayed_attempts": len(data["attempts"]),
        "first_run_lifecycle": first["lifecycle"]["state"],
        "fork_state": second_forecast["execution"]["state"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--sledra-bin", required=True, type=Path)
    args = parser.parse_args()
    args.workspace.mkdir(parents=True, exist_ok=True)
    summary = {
        "equivalence": check_equivalence(args.bundle, args.workspace, args.sledra_bin),
        "identities": check_identities(args.bundle, args.workspace, args.sledra_bin),
    }
    (args.workspace / "acceptance.json").write_text(
        json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print("SLD-18 local acceptance passed:", args.workspace / "acceptance.json")


if __name__ == "__main__":
    main()
