"""Regenerate deterministic, synthetic cross-language contract fixtures."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

from check import canonical, content_sha, length_digest, registry_id, sha, task_instance_id


OUT = Path(__file__).parent / "fixtures" / "1.0.0"
T = "2026-10-10T03:12:14+08:00"


def write(name: str, data: object) -> None:
    (OUT / name).write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n")


def event(kind: str, event_id: str, role: str, association: dict, payload: dict,
          *, source_kind: str = "direct", occurred_at: str | None = T,
          evidence: list | None = None, extensions: dict | None = None) -> dict:
    result = {
        "schema_version": "1.0.0", "kind": "event", "event_id": event_id,
        "event_type": kind,
        "producer": {"name": "synthetic-contract-helper", "version": "0.1.0", "role": role},
        "source": {"kind": source_kind, "source_id": "sandbox-node-a", "session_id": "session-a"},
        "occurred_at": occurred_at, "recorded_at": T,
        "time_quality": {"basis": "producer_clock" if occurred_at else "unknown", **({} if occurred_at else {"reason": "source time unavailable"})},
        "association": association, "payload": payload,
    }
    if evidence is not None:
        result["evidence_refs"] = evidence
    if extensions is not None:
        result["extensions"] = extensions
    return result


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cycle = {"cycle_time": "2026-10-09T16:00:00Z", "cycle_kind": "forecast"}
    manifest = {
        "schema_version": "1.0.0", "kind": "definition_manifest",
        "generator": {"name": "synthetic-generator", "version": "0.1.0"},
        "application_revision": "synthetic-revision-1", "mapping_version": "1",
        "tasks": [{
            "task_key": "main.forecast", "task_kind": "forecast", "stage_key": "forecast",
            "parameters": {
                "member": {"type": "string", "required": True},
                "lead_seconds": {"type": "integer", "required": True},
                "domain": {"type": "string", "required": False, "default": "global"},
            },
            "engine_path_template": "/synthetic/main/{member}/forecast", "depends_on": [],
        }],
        "stage_definitions": ["forecast"], "product_rule_refs": [],
    }
    manifest["content_sha256"] = content_sha(manifest, ())
    manifest["definition_id"] = "def_" + manifest["content_sha256"]
    expectation = {
        "schema_version": "1.0.0", "kind": "expectation",
        "system_id": "synthetic.model", "purpose": "test", "deployment_id": "wsl-sandbox",
        "approved_by_ref": "synthetic-only", "cycles": [{"cycle": cycle, "status": "expected", "reason": None}],
        "slots": [{"slot_id": "grib.member01.lead0", "artifact_id": "grib.member01.lead0", "rule_id": "file-exists", "rule_version": "1", "deadline_at": T, "recipient": "synthetic-recipient"}],
    }
    exp_sha = content_sha(expectation, ())
    expectation["plan_id"] = "exp_" + exp_sha
    registry = "registry-synthetic-1"
    launch_token = "launch-coldstart-20261010"
    run_id = registry_id(registry, "run", "wsl-sandbox", launch_token)
    request = {"cycle": cycle, "system_id": "synthetic.model", "purpose": "test", "deployment_id": "wsl-sandbox", "launch_token": launch_token}
    run = {
        "schema_version": "1.0.0", "kind": "run_context",
        "registry_id": registry, "run_id": run_id,
        "request_sha256": sha(request), "launch_token_sha256": hashlib.sha256(launch_token.encode()).hexdigest(),
        "system_id": "synthetic.model", "purpose": "test", "deployment_id": "wsl-sandbox", "platform_id": "wsl2",
        "cycle": cycle,
        "definition_ref": {"id": manifest["definition_id"], "sha256": manifest["content_sha256"]},
        "expectation_ref": {"id": expectation["plan_id"], "sha256": exp_sha},
        "config_refs": [], "scope": [
            {"task_key": "main.forecast", "parameters": {"member": "01", "lead_seconds": 0, "domain": "global"}},
            {"task_key": "main.forecast", "parameters": {"member": "02", "lead_seconds": 0, "domain": "global"}},
        ], "rerun_of": None,
    }
    run["context_sha256"] = content_sha(run, ())
    params = run["scope"][0]["parameters"]
    ti_id = task_instance_id(run_id, "main.forecast", params)
    execution_token = "exec-member01-first"
    attempt_id = registry_id(registry, "attempt", ti_id, execution_token)
    attempt = {
        "schema_version": "1.0.0", "kind": "attempt_context", "run_id": run_id,
        "run_context_sha256": run["context_sha256"], "task_key": "main.forecast", "parameters": params,
        "task_instance_id": ti_id, "attempt_id": attempt_id,
        "execution_token_sha256": hashlib.sha256(execution_token.encode()).hexdigest(),
        "retry_of": None, "process_role": "forecast_program",
    }
    attempt["context_sha256"] = content_sha(attempt, ())
    job = {
        "schema_version": "1.0.0", "kind": "job_ref",
        "platform_id": "wsl2", "scheduler": "synthetic-scheduler", "cluster_id": "sandbox-cluster",
        "scheduler_job_id": "42", "submission_instance": "submit-a",
        "run_id": run_id, "attempt_id": attempt_id, "role": "primary",
    }
    job["job_ref_id"] = "job_" + length_digest(job["platform_id"], job["scheduler"], job["cluster_id"],
                                                   job["scheduler_job_id"], job["submission_instance"])
    artifact = {
        "schema_version": "1.0.0", "kind": "artifact", "artifact_id": "grib.member01.lead0",
        "artifact_kind": "forecast_grib", "cycle": cycle, "lead_seconds": 0,
        "member": "01", "domain": "global",
        "version_evidence": {"kind": "content_sha256", "value": "a" * 64},
        "location_refs": [{"locator": "opaque:synthetic-grib-01", "kind": "opaque"}],
        "producer_run_id": run_id, "producer_attempt_id": attempt_id,
    }
    artifact["artifact_version_id"] = "av_" + content_sha(artifact, ())
    run_assoc = {"run_id": run_id}
    attempt_assoc = {"run_id": run_id, "task_instance_id": ti_id, "attempt_id": attempt_id}
    job_attempt_assoc = {**attempt_assoc, "job_ref_id": job["job_ref_id"]}
    job_run_assoc = {**run_assoc, "job_ref_id": job["job_ref_id"]}
    eid = lambda name: "evt_" + hashlib.sha256((run_id + name).encode()).hexdigest()
    events = [
        event("run.registered", eid("run.registered"), "registry", run_assoc,
              {"run_context_sha256": run["context_sha256"], "request_sha256": run["request_sha256"]}),
        event("attempt.registered", eid("attempt.registered"), "registry", attempt_assoc,
              {"attempt_context_sha256": attempt["context_sha256"], "execution_token_sha256": attempt["execution_token_sha256"], "retry_of": None}),
        event("attempt.started", "be998bc6-86a5-4e84-9e14-6c4c950da121", "process_owner", attempt_assoc,
              {"invocation_id": "invocation-a", "process_role": "forecast_program"}),
        event("stage.started", "be998bc6-86a5-4e84-9e14-6c4c950da122", "stage_owner", attempt_assoc,
              {"stage_key": "forecast", "interval_id": "interval-a"}),
        event("stage.ended", "be998bc6-86a5-4e84-9e14-6c4c950da123", "stage_owner", attempt_assoc,
              {"stage_key": "forecast", "interval_id": "interval-a", "result": "failure", "duration_ns": 1000000000}),
        event("attempt.exited", "be998bc6-86a5-4e84-9e14-6c4c950da124", "process_owner", attempt_assoc,
              {"termination": "exited", "exit_code": 2, "process_role": "forecast_program"}),
        event("run.finish_declared", eid("run.finish_declared"), "lifecycle_owner", run_assoc,
              {"declaration_id": "finish-a", "scope_ref": "scope-a", "rule_version": "1", "covered_attempt_ids": [attempt_id], "covered_attempts_sha256": sha([attempt_id]), "reason": "synthetic end", "result": "finished"}),
        event("artifact.validation_completed", "be998bc6-86a5-4e84-9e14-6c4c950da125", "artifact_checker", run_assoc,
              {"artifact_version_ref": artifact["artifact_version_id"], "rule_id": "file-exists", "rule_version": "1", "check_id": "check-a", "result": "fail"},
              source_kind="observed", evidence=[{"evidence_id": "synthetic-evidence-a", "locator": "opaque:synthetic-check-a",
                                                "source": "synthetic-checker", "availability": "present"}]),
        event("submission.accepted", "be998bc6-86a5-4e84-9e14-6c4c950da127", "submitter", job_attempt_assoc,
              {"submission_instance": job["submission_instance"], "job_ref_id": job["job_ref_id"], "confirmation_basis": "synthetic-return"}),
        event("job.state_observed", "be998bc6-86a5-4e84-9e14-6c4c950da128", "scheduler_observer", job_run_assoc,
              {"job_ref_id": job["job_ref_id"], "raw_state": "DONE", "normalized_state": "finished", "observation_time": T}, source_kind="observed"),
        event("delivery.confirmed", "be998bc6-86a5-4e84-9e14-6c4c950da129", "delivery_verifier", run_assoc,
              {"expectation_slot": "grib.member01.lead0", "artifact_version_ref": artifact["artifact_version_id"],
               "rule_version": "1", "recipient": "synthetic-recipient", "check_id": "delivery-a", "confirmed_at": T},
              source_kind="observed", evidence=[{"evidence_id": "synthetic-delivery-evidence", "locator": "opaque:synthetic-delivery-a",
                                                "source": "synthetic-recipient", "availability": "present"}]),
    ]
    bundle = {"definition_manifest": manifest, "run_context": run, "attempt_context": attempt,
              "job_ref": job, "artifact": artifact, "expectation": expectation, "events": events}
    write("valid-bundle.json", bundle)
    negative_zero = (OUT / "valid-bundle.json").read_text().replace('"lead_seconds": 0', '"lead_seconds": -0')
    (OUT / "valid-negative-zero.json").write_text(negative_zero)
    extended = copy.deepcopy(bundle)
    extended["events"].append(event("future.metric_recorded", "be998bc6-86a5-4e84-9e14-6c4c950da126", "future_role", run_assoc,
                                    {"future_value": 7}, extensions={"example.future": {"opaque": [1, 2, 3], "emoji": "😀"}}))
    extended["events"][-1]["schema_version"] = "1.1.0"
    write("valid-future-extension.json", extended)
    escaped = (OUT / "valid-future-extension.json").read_text().replace("😀", "\\ud83d\\ude00")
    (OUT / "valid-surrogate-escape.json").write_text(escaped)
    (OUT / "invalid-unpaired-surrogate.json").write_text(escaped.replace("\\ud83d\\ude00", "\\ud800"))

    def invalid(name: str, change) -> None:
        bad = copy.deepcopy(bundle); change(bad); write(name, bad)

    invalid("invalid-missing-task-parameter.json", lambda b: b["run_context"]["scope"][0]["parameters"].pop("member"))
    invalid("invalid-unexpanded-default.json", lambda b: b["run_context"]["scope"][0]["parameters"].pop("domain"))
    invalid("invalid-timezone.json", lambda b: b["events"][2].update(occurred_at="2026-10-10T03:12:14"))
    invalid("invalid-exit-fields.json", lambda b: b["events"][5]["payload"].pop("exit_code"))
    invalid("invalid-role.json", lambda b: b["events"][5]["producer"].update(role="engine_observer"))
    invalid("invalid-termination-enum.json", lambda b: b["events"][5]["payload"].update(termination="completed"))
    invalid("invalid-extra-core-field.json", lambda b: b["events"][5].update(secret_token="must-not-be-here"))
    invalid("invalid-unknown-parameter.json", lambda b: b["attempt_context"]["parameters"].update(hidden_dimension="x"))
    invalid("invalid-job-parent.json", lambda b: b["job_ref"].update(attempt_id="att_" + "0" * 64))
    invalid("invalid-job-generation.json", lambda b: b["job_ref"].update(submission_instance="submit-b"))
    invalid("invalid-submission-generation.json", lambda b: b["events"][8]["payload"].update(submission_instance="submit-b"))
    invalid("invalid-validation-artifact.json", lambda b: b["events"][7]["payload"].update(artifact_version_ref="av_wrong"))
    invalid("invalid-delivery-slot.json", lambda b: b["events"][10]["payload"].update(expectation_slot="unknown-slot"))
    invalid("invalid-delivery-recipient.json", lambda b: b["events"][10]["payload"].update(recipient="wrong-recipient"))
    invalid("invalid-artifact-version-evidence.json", lambda b: b["artifact"]["version_evidence"].update(value="not-a-sha256"))
    def bad_enum(b: dict) -> None:
        doc = b["definition_manifest"]
        doc["tasks"][0]["parameters"]["member"]["enum"] = ["01", 2]
        doc["content_sha256"] = content_sha(doc, ("content_sha256", "definition_id"))
        doc["definition_id"] = "def_" + doc["content_sha256"]

    invalid("invalid-enum-definition.json", bad_enum)

    def duplicate_cycle(b: dict) -> None:
        doc = b["expectation"]
        doc["cycles"].append(copy.deepcopy(doc["cycles"][0]))
        digest = content_sha(doc, ("plan_id",))
        doc["plan_id"] = "exp_" + digest
        run_doc = b["run_context"]
        run_doc["expectation_ref"] = {"id": doc["plan_id"], "sha256": digest}
        run_doc["context_sha256"] = content_sha(run_doc, ("context_sha256",))

    invalid("invalid-duplicate-expected-cycle.json", duplicate_cycle)
    invalid("invalid-missing-association.json", lambda b: b["events"][5]["association"].pop("attempt_id"))
    invalid("invalid-missing-evidence.json", lambda b: b["events"][7].pop("evidence_refs"))
    invalid("invalid-parent.json", lambda b: b["events"][5]["association"].update(run_id="run_wrong"))
    invalid("invalid-major.json", lambda b: b["events"][5].update(schema_version="2.0.0"))
    invalid("invalid-uuid-version.json", lambda b: b["events"][5].update(event_id="be998bc6-86a5-1e84-9e14-6c4c950da124"))
    invalid("invalid-semver-leading-zero.json", lambda b: b["events"][5].update(schema_version="1.01.0"))
    invalid("invalid-conflicting-event-id.json", lambda b: b["events"].append({**copy.deepcopy(b["events"][5]), "payload": {"termination": "exited", "exit_code": 0, "process_role": "forecast_program"}}))
    invalid("invalid-task-success.json", lambda b: b["events"][5].update(event_type="task.success"))
    duplicate = (OUT / "valid-bundle.json").read_text().replace('"schema_version": "1.0.0"', '"schema_version": "1.0.0", "schema_version": "1.0.0"', 1)
    (OUT / "invalid-duplicate-key.json").write_text(duplicate)
    vectors = {
        "canonical_parameters": canonical(params).decode(),
        "task_instance_id": ti_id,
        "run_id": run_id, "attempt_id": attempt_id,
        "job_ref_id": job["job_ref_id"],
        "reused_scheduler_job_id_ref": "job_" + length_digest(job["platform_id"], job["scheduler"], job["cluster_id"],
                                                                 job["scheduler_job_id"], "submit-b"),
        "manifest_sha256": manifest["content_sha256"],
        "run_context_sha256": run["context_sha256"],
        "attempt_context_sha256": attempt["context_sha256"],
        "event_sha256": sha(events[5]),
        "unicode_nfc": canonical({"label": "e\u0301", "emoji": "☀"}).decode(),
        "key_order": canonical({"z": 1, "a": 2}).decode(),
        "string_01": canonical({"member": "01"}).decode(),
        "integer_1": canonical({"member": 1}).decode(),
        "zero_hour_seconds": 0,
        "negative_zero_canonical": canonical(json.loads('{"n":-0}')).decode(),
        "lead_inputs": [{"value": 0, "unit": "h", "normalized_lead_seconds": 0},
                        {"value": 0, "unit": "s", "normalized_lead_seconds": 0}],
    }
    write("golden-vectors.json", vectors)
    cases = {
        "valid-bundle.json": "ok", "valid-future-extension.json": "ok", "valid-surrogate-escape.json": "ok",
        "valid-negative-zero.json": "ok",
        "invalid-unpaired-surrogate.json": "schema_invalid",
        "invalid-missing-task-parameter.json": "missing_parameter",
        "invalid-unexpanded-default.json": "missing_parameter",
        "invalid-timezone.json": "schema_invalid",
        "invalid-exit-fields.json": "semantic_invalid",
        "invalid-role.json": "unauthorized_event",
        "invalid-termination-enum.json": "schema_invalid",
        "invalid-extra-core-field.json": "schema_invalid",
        "invalid-unknown-parameter.json": "unknown_parameter",
        "invalid-job-parent.json": "parent_mismatch",
        "invalid-job-generation.json": "semantic_invalid",
        "invalid-submission-generation.json": "parent_mismatch",
        "invalid-validation-artifact.json": "parent_mismatch",
        "invalid-delivery-slot.json": "parent_mismatch",
        "invalid-delivery-recipient.json": "parent_mismatch",
        "invalid-artifact-version-evidence.json": "semantic_invalid",
        "invalid-enum-definition.json": "semantic_invalid",
        "invalid-duplicate-expected-cycle.json": "semantic_invalid",
        "invalid-missing-association.json": "parent_mismatch",
        "invalid-missing-evidence.json": "semantic_invalid",
        "invalid-parent.json": "parent_mismatch",
        "invalid-major.json": "unsupported_major",
        "invalid-uuid-version.json": "semantic_invalid",
        "invalid-semver-leading-zero.json": "schema_invalid",
        "invalid-conflicting-event-id.json": "conflicting_event_id",
        "invalid-task-success.json": "semantic_invalid",
        "invalid-duplicate-key.json": "duplicate_key",
    }
    write("cases.json", cases)
    lines = [f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}" for path in sorted(OUT.glob("*.json"))]
    (OUT / "SHA256SUMS").write_text("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
