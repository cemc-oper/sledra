"""Explicit launch, attempt and finish owners for the generated local workflow.

Only the installed recorder and Shell API write Sledra facts. The mock scheduler
emits JobRef documents for accepted submissions; no JobRef CLI is available.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import unicodedata
from pathlib import Path


HERE = Path(__file__).resolve().parent
SHELL_API = HERE.parents[1] / "integrations/bash/sledra.sh"
TOKEN = re.compile(r"[A-Za-z0-9_-]+\Z")
TASKS = ("prepare", "forecast", "verify")


def save_once(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n"
    try:
        with path.open("x", encoding="utf-8") as file:
            file.write(encoded)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != encoded:
            raise ValueError(f"conflicting request at {path}") from None


def append_json(path: Path, value: dict) -> None:
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n")


def job_ref(context: dict, scheduler_job_id: str, submission_instance: str) -> dict:
    parts = ("local", "synthetic-scheduler", "mock-cluster", scheduler_job_id, submission_instance)
    digest = hashlib.sha256()
    for part in parts:
        encoded = unicodedata.normalize("NFC", part).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return {
        "schema_version": "1.0.0",
        "kind": "job_ref",
        "job_ref_id": "job_" + digest.hexdigest(),
        "platform_id": parts[0],
        "scheduler": parts[1],
        "cluster_id": parts[2],
        "scheduler_job_id": scheduler_job_id,
        "submission_instance": submission_instance,
        "run_id": context["run_id"],
        "attempt_id": context["attempt_id"],
        "role": "primary",
    }


class Harness:
    def __init__(
        self,
        bundle: Path,
        workspace: Path,
        sledra_bin: Path,
        *,
        enabled: bool,
        shell_api: Path = SHELL_API,
    ):
        self.bundle = bundle.resolve()
        self.workspace = workspace.resolve()
        self.sledra_bin = sledra_bin.resolve()
        self.shell_api = shell_api.resolve()
        self.enabled = enabled
        self.root = self.workspace / "sledra"
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.root / "invocations").mkdir(exist_ok=True)

    def invocation(self, launch_token: str) -> Path:
        if not TOKEN.fullmatch(launch_token):
            raise ValueError("launch token must contain only letters, digits, _ or -")
        path = self.root / "invocations" / launch_token
        path.mkdir(parents=True, exist_ok=True)
        return path

    def context(self, launch_token: str) -> Path:
        return self.invocation(launch_token) / "contexts" / "run.json"

    def call(self, invocation: Path, *args: str) -> bool:
        if not self.enabled:
            return False
        env = os.environ.copy()
        env["SLEDRA_ROOT"] = str(self.root)
        try:
            result = subprocess.run(
                [str(self.sledra_bin), *args], env=env, capture_output=True, check=False
            )
        except OSError:
            append_json(invocation / "observation.jsonl", {"code": "tool_unavailable"})
            return False
        if result.returncode:
            append_json(invocation / "observation.jsonl", {"code": "recorder_failed"})
            return False
        return True

    def launch(
        self, launch_token: str, cycle_time: str, *, member: str = "01", lead_seconds: int = 0
    ) -> Path:
        invocation = self.invocation(launch_token)
        scope = [
            {"task_key": "main.prepare", "parameters": {}},
            {
                "task_key": "main.forecast",
                "parameters": {"member": member, "lead_seconds": lead_seconds},
            },
            {
                "task_key": "main.verify",
                "parameters": {"member": member, "lead_seconds": lead_seconds},
            },
        ]
        request = {
            "registry_id": "sld18-local",
            "launch_token": launch_token,
            "system_id": "synthetic.workflow",
            "purpose": "test",
            "deployment_id": "local",
            "platform_id": "local",
            "cycle": {"cycle_kind": "forecast", "cycle_time": cycle_time},
            "definition_file": str(self.bundle / "definition_manifest.json"),
            "scope": scope,
        }
        path = invocation / "inputs/run.json"
        save_once(path, request)
        context = self.context(launch_token)
        context.parent.mkdir(parents=True, exist_ok=True)
        self.call(
            invocation,
            "registry",
            "run-create",
            "--request-file",
            str(path),
            "--context-out",
            str(context),
        )
        return context

    def attempt(
        self,
        launch_token: str,
        task: str,
        execution_token: str,
        *,
        retry_of: str | None = None,
        mode: str = "accept",
        fail_forecast: bool = False,
        event_bin: Path | None = None,
        context_override: Path | None = None,
    ) -> dict:
        if task not in TASKS or not TOKEN.fullmatch(execution_token):
            raise ValueError("invalid task or execution token")
        if mode not in (
            "accept",
            "reject",
            "timeout",
            "start-before-return",
            "accept-record-failure",
        ):
            raise ValueError("invalid mock submission mode")
        invocation = self.invocation(launch_token)
        attempt_dir = invocation / "attempts" / f"{task}-{execution_token}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        intent = {
            "task": task,
            "execution_token": execution_token,
            "retry_of": retry_of,
            "mode": mode,
            "fail_forecast": fail_forecast,
            "event_bin": str(event_bin.resolve()) if event_bin else None,
            "context_override": (str(context_override.resolve()) if context_override else None),
        }
        intent_path = attempt_dir / "intent.json"
        if intent_path.exists():
            if json.loads(intent_path.read_text()) != intent:
                raise ValueError("same execution token has a conflicting request")
            result_path = attempt_dir / "result.json"
            if not result_path.exists():
                raise RuntimeError("submission outcome indeterminate; do not rerun payload")
            return json.loads(result_path.read_text())
        save_once(intent_path, intent)

        run_request = json.loads((invocation / "inputs/run.json").read_text())
        scope = {item["task_key"]: item["parameters"] for item in run_request["scope"]}
        request = {
            "task_key": f"main.{task}",
            "parameters": scope[f"main.{task}"],
            "execution_token": execution_token,
            "retry_of": retry_of,
            "process_role": f"{task}_program",
        }
        request_path = attempt_dir / "attempt-request.json"
        save_once(request_path, request)
        context = invocation / "contexts" / task / f"{execution_token}.json"
        context.parent.mkdir(parents=True, exist_ok=True)
        if self.context(launch_token).is_file():
            self.call(
                invocation,
                "registry",
                "attempt-create",
                "--context",
                str(self.context(launch_token)),
                "--request-file",
                str(request_path),
                "--context-out",
                str(context),
            )
        attempt_id = json.loads(context.read_text())["attempt_id"] if context.is_file() else None

        process_config = attempt_dir / "process-config.json"
        stage_config = attempt_dir / "stage-config.json"
        for path, role in ((process_config, "process_owner"), (stage_config, "stage_owner")):
            save_once(
                path,
                {
                    "producer_name": "sld18-generated",
                    "producer_version": "1",
                    "producer_role": role,
                    "source_id": "sld18-local",
                    "session_id": f"{launch_token}-{task}-{execution_token}-{role}",
                },
            )
        effective_context = context_override.resolve() if context_override else context
        environment = os.environ.copy()
        environment.update(
            {
                "SLEDRA_ROOT": str(self.root),
                "SLEDRA_BIN": str(event_bin or self.sledra_bin),
                "SLEDRA_SHELL": str(self.shell_api),
                "SLEDRA_ENABLED": "1" if self.enabled else "0",
                "SLEDRA_CONFIG": str(process_config),
                "STAGE_CONFIG": str(stage_config),
                "SLEDRA_CONTEXT": str(effective_context),
                "OBS_DIAGNOSTICS": str(invocation / "observation-shell.log"),
                "PRODUCT_DIR": str(invocation / "products"),
                "EXPECTED_PRODUCT": str(self.bundle / "expected_product.json"),
                "VALIDATION_RESULT": str(attempt_dir / "validation.json"),
                "EXECUTION_LOG": str(invocation / "executions.log"),
                "CALLBACK_LOG": str(invocation / "callbacks.log"),
                "CONTEXT_PATH_LOG": str(invocation / "context-paths.log"),
                "EXECUTION_TOKEN": execution_token,
                "FORECAST_FAIL": "1" if fail_forecast else "0",
            }
        )
        job = self.bundle / "jobs" / f"{task}.sh"
        submission_log = invocation / "mock-submissions.jsonl"
        append_json(
            submission_log,
            {
                "phase": "submit",
                "token": execution_token,
                "argv": [str(job)],
                "attempt_id": attempt_id,
            },
        )
        result = {
            "attempt_id": attempt_id,
            "context": str(context),
            "task": task,
            "token": execution_token,
            "job_id": None,
            "job_ref_id": None,
            "submission_outcome": "unknown",
            "payload_rc": None,
            "stdout": "",
            "stderr": "",
        }
        if mode == "reject":
            result["submission_outcome"] = "rejected"
        elif mode == "timeout":
            pass
        else:
            job_id = f"mock-{launch_token}-{execution_token}"
            job_document = (
                job_ref(json.loads(context.read_text()), job_id, execution_token)
                if attempt_id
                else None
            )
            job_path = self.root / "mock-jobs" / f"{launch_token}-{execution_token}.json"

            def record_acceptance() -> bool:
                if job_document is None:
                    return True
                try:
                    save_once(job_path, job_document)
                except OSError:
                    append_json(
                        submission_log, {"phase": "record-failure", "token": execution_token}
                    )
                    return False
                return True

            if mode == "accept":
                append_json(
                    submission_log,
                    {"phase": "accepted", "token": execution_token, "job_id": job_id},
                )
                recorded = record_acceptance()
            else:
                recorded = True
            completed = subprocess.run(
                [str(job)], env=environment, capture_output=True, text=True, check=False
            )
            result.update(
                payload_rc=completed.returncode, stdout=completed.stdout, stderr=completed.stderr
            )
            if mode == "accept" and recorded:
                result.update(
                    job_id=job_id,
                    job_ref_id=job_document["job_ref_id"] if job_document else None,
                    submission_outcome="accepted",
                )
            elif mode == "accept-record-failure":
                append_json(submission_log, {"phase": "record-failure", "token": execution_token})
            if mode == "start-before-return":
                append_json(submission_log, {"phase": "payload-returned", "token": execution_token})
                append_json(
                    submission_log,
                    {"phase": "accepted", "token": execution_token, "job_id": job_id},
                )
                if record_acceptance():
                    result.update(
                        job_id=job_id,
                        job_ref_id=job_document["job_ref_id"] if job_document else None,
                        submission_outcome="accepted",
                    )
        save_once(attempt_dir / "result.json", result)
        return result

    def finish(
        self, launch_token: str, declaration_id: str, covered_attempt_ids: list[str]
    ) -> bool:
        invocation = self.invocation(launch_token)
        request = {
            "declaration_id": declaration_id,
            "rule_version": "1",
            "covered_attempt_ids": covered_attempt_ids,
            "reason": "synthetic local scope",
            "result": "finished",
            "source_id": "sld18-local",
            "session_id": launch_token + "-owner",
        }
        path = invocation / "inputs" / f"finish-{declaration_id}.json"
        save_once(path, request)
        if not self.context(launch_token).is_file():
            return False
        return self.call(
            invocation,
            "run",
            "finish",
            "--context",
            str(self.context(launch_token)),
            "--scope-file",
            str(path),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--sledra-bin", type=Path, required=True)
    parser.add_argument("--shell-api", type=Path, default=SHELL_API)
    parser.add_argument("--enabled", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    launch = sub.add_parser("launch")
    launch.add_argument("--launch-token", required=True)
    launch.add_argument("--cycle-time", required=True)
    launch.add_argument("--member", default="01")
    launch.add_argument("--lead-seconds", type=int, default=0)
    attempt = sub.add_parser("attempt")
    attempt.add_argument("--launch-token", required=True)
    attempt.add_argument("--task", choices=TASKS, required=True)
    attempt.add_argument("--execution-token", required=True)
    attempt.add_argument("--retry-of")
    attempt.add_argument(
        "--mode",
        default="accept",
        choices=("accept", "reject", "timeout", "start-before-return", "accept-record-failure"),
    )
    attempt.add_argument("--fail-forecast", action="store_true")
    attempt.add_argument("--event-bin", type=Path)
    attempt.add_argument("--context-override", type=Path)
    finish = sub.add_parser("finish")
    finish.add_argument("--launch-token", required=True)
    finish.add_argument("--declaration-id", required=True)
    finish.add_argument("--attempt-id", action="append", default=[])
    args = parser.parse_args()
    harness = Harness(
        args.bundle, args.workspace, args.sledra_bin, enabled=args.enabled, shell_api=args.shell_api
    )
    if args.command == "launch":
        harness.launch(
            args.launch_token, args.cycle_time, member=args.member, lead_seconds=args.lead_seconds
        )
    elif args.command == "attempt":
        result = harness.attempt(
            args.launch_token,
            args.task,
            args.execution_token,
            retry_of=args.retry_of,
            mode=args.mode,
            fail_forecast=args.fail_forecast,
            event_bin=args.event_bin,
            context_override=args.context_override,
        )
        sys.stdout.write(result["stdout"])
        sys.stderr.write(result["stderr"])
        if result["payload_rc"] is not None:
            raise SystemExit(result["payload_rc"])
    else:
        harness.finish(args.launch_token, args.declaration_id, args.attempt_id)


if __name__ == "__main__":
    main()
