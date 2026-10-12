"""Exercise the SLD-05 CLI with synthetic Run/Attempt/finish inputs."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def save(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    fixture = json.loads((ROOT / "spec/fixtures/1.0.0/valid-bundle.json").read_text())
    with tempfile.TemporaryDirectory(prefix="sledra-context-") as scratch:
        temp = Path(scratch)
        binary = temp / "sledra"
        env = dict(os.environ, GOPROXY="off", GOSUMDB="off", SLEDRA_ROOT=str(temp),
                   SLEDRA_SPEC_DIR=str(ROOT / "spec/1.0.0"))
        subprocess.run(["go", "build", "-o", str(binary), "./cmd/sledra"],
                       cwd=ROOT / "recorder", env=env, check=True)

        def call(*args: str, code: str | None = None) -> None:
            completed = subprocess.run([str(binary), *args], cwd=temp, env=env,
                                       capture_output=True)
            assert completed.stdout == b"", completed.stdout
            if code is None:
                assert completed.returncode == 0 and completed.stderr == b"", completed.stderr
            else:
                assert completed.returncode == 1, completed.stderr
                assert json.loads(completed.stderr) == {"ok": False, "code": code}

        definition = temp / "definition.json"
        expectation = temp / "expectation.json"
        save(definition, fixture["definition_manifest"])
        save(expectation, fixture["expectation"])
        base = fixture["run_context"]
        request = {
            "registry_id": base["registry_id"], "system_id": base["system_id"],
            "purpose": base["purpose"], "deployment_id": base["deployment_id"],
            "platform_id": base["platform_id"], "cycle": base["cycle"],
            "definition_file": str(definition), "expectation_file": str(expectation),
            "scope": base["scope"],
        }
        runs = []
        for index, token in enumerate(("launch-a", "launch-b"), 1):
            request["launch_token"] = token
            req = temp / f"run-request-{index}.json"
            ctx = temp / f"run-{index}.json"
            save(req, request)
            call("registry", "run-create", "--request-file", str(req), "--context-out", str(ctx))
            call("context", "validate", "--file", str(ctx))
            runs.append((ctx, json.loads(ctx.read_text())))
        assert runs[0][1]["run_id"] != runs[1][1]["run_id"]
        assert runs[0][1]["cycle"] == runs[1][1]["cycle"]

        attempts = []
        for index, token in enumerate(("exec-a1", "exec-a2", "exec-a3"), 1):
            req = temp / f"attempt-request-{index}.json"
            ctx = temp / f"attempt-{index}.json"
            save(req, {"task_key": "main.forecast", "parameters":
                       {"member": "01", "lead_seconds": 0}, "execution_token": token,
                       "retry_of": attempts[-1][1]["attempt_id"] if attempts else None,
                       "process_role": "forecast_program"})
            if index == 3:
                break
            call("registry", "attempt-create", "--context", str(runs[0][0]),
                 "--request-file", str(req), "--context-out", str(ctx))
            call("context", "validate", "--file", str(ctx))
            attempts.append((ctx, json.loads(ctx.read_text())))

        finish_request = temp / "finish-request.json"
        save(finish_request, {"declaration_id": "finish-1", "rule_version": "1",
                              "covered_attempt_ids": [item[1]["attempt_id"] for item in attempts],
                              "reason": "synthetic completion", "result": "finished",
                              "source_id": "synthetic-cli", "session_id": "session-1"})
        call("run", "finish", "--context", str(runs[0][0]),
             "--scope-file", str(finish_request))
        finish_files = list((temp / "finish").rglob("*.json"))
        assert len(finish_files) == 1
        before = finish_files[0].read_bytes()

        call("registry", "attempt-create", "--context", str(runs[0][0]),
             "--request-file", str(req), "--context-out", str(ctx))
        a3 = json.loads(ctx.read_text())
        assert a3["run_id"] == runs[0][1]["run_id"]
        assert a3["retry_of"] == attempts[-1][1]["attempt_id"]
        call("run", "finish", "--context", str(runs[0][0]),
             "--scope-file", str(finish_request))
        assert finish_files[0].read_bytes() == before
        assert a3["attempt_id"].encode() not in before
        call("context", "validate", "--file", str(temp / "missing.json"),
             code="context_missing")
        print("SLD-05 CLI lifecycle: 2 Runs, 3 Attempts, immutable finish, empty stdout: pass")


if __name__ == "__main__":
    main()
