"""Build the checked-in Sledra 1.0 draft schemas and event catalog.

The generated JSON is the portable contract. This script is an authoring aid;
validators load only local checked-in JSON and never fetch a schema URL.
"""

from __future__ import annotations

import json
from pathlib import Path


OUT = Path(__file__).parent / "1.0.0"
META = "https://json-schema.org/draft/2020-12/schema"
STR = {"type": "string", "minLength": 1}
INT = {"type": "integer", "minimum": 0, "maximum": 9223372036854775807}
SHA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
TIME = {"type": "string", "format": "date-time"}
PARAM = {"oneOf": [{"type": "string"}, {"type": "integer", "minimum": -9223372036854775808, "maximum": 9223372036854775807}, {"type": "boolean"}]}
SNAKE = "^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$"
PARAMS = {"type": "object", "propertyNames": {"pattern": SNAKE}, "additionalProperties": PARAM}
EXT = {"type": "object", "propertyNames": {"pattern": "^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$"}, "additionalProperties": {"type": "object"}}


def obj(required: list[str], props: dict, *, extra: bool = False) -> dict:
    return {"type": "object", "required": required, "properties": props, "additionalProperties": extra}


def document(name: str, required: list[str], props: dict) -> None:
    schema = {"$schema": META, "title": name, **obj(["schema_version", "kind", *required], {
        "schema_version": {"type": "string", "pattern": r"^1\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"},
        "kind": {"const": name},
        **props,
        "extensions": EXT,
    })}
    (OUT / f"{name}.schema.json").write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n")


def payload(required: list[str], props: dict) -> dict:
    return {"$schema": META, **obj(required, {**props, "extensions": EXT})}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    ref = obj(["id", "sha256"], {"id": STR, "sha256": SHA})
    cycle = obj(["cycle_time", "cycle_kind"], {"cycle_time": TIME, "cycle_kind": {"enum": ["forecast", "assimilation"]}})
    task_param = obj(["type", "required"], {
        "type": {"enum": ["string", "integer", "boolean"]},
        "required": {"type": "boolean"},
        "default": PARAM,
        "enum": {"type": "array", "minItems": 1, "items": PARAM},
    })
    task = obj(["task_key", "task_kind", "stage_key", "parameters", "engine_path_template", "depends_on"], {
        "task_key": STR, "task_kind": STR, "stage_key": STR,
        "parameters": {"type": "object", "propertyNames": {"pattern": SNAKE}, "additionalProperties": task_param},
        "engine_path_template": STR, "depends_on": {"type": "array", "uniqueItems": True, "items": STR},
    })
    document("definition_manifest", ["definition_id", "content_sha256", "generator", "application_revision", "mapping_version", "tasks", "stage_definitions", "product_rule_refs"], {
        "definition_id": STR, "content_sha256": SHA,
        "generator": obj(["name", "version"], {"name": STR, "version": STR}),
        "application_revision": STR, "mapping_version": STR,
        "tasks": {"type": "array", "minItems": 1, "items": task},
        "stage_definitions": {"type": "array", "minItems": 1, "uniqueItems": True, "items": STR},
        "product_rule_refs": {"type": "array", "items": ref},
    })
    scope_entry = obj(["task_key", "parameters"], {"task_key": STR, "parameters": PARAMS})
    document("run_context", ["registry_id", "run_id", "request_sha256", "launch_token_sha256", "system_id", "purpose", "deployment_id", "platform_id", "cycle", "definition_ref", "expectation_ref", "config_refs", "scope", "context_sha256"], {
        "registry_id": STR, "run_id": {"type": "string", "pattern": "^run_[0-9a-f]{64}$"},
        "request_sha256": SHA, "launch_token_sha256": SHA,
        "system_id": STR, "purpose": STR, "deployment_id": STR, "platform_id": STR,
        "cycle": cycle, "definition_ref": ref,
        "expectation_ref": {"oneOf": [ref, {"type": "null"}]},
        "config_refs": {"type": "array", "items": ref},
        "scope": {"type": "array", "minItems": 1, "items": scope_entry},
        "context_sha256": SHA, "rerun_of": {"oneOf": [{"type": "string", "pattern": "^run_[0-9a-f]{64}$"}, {"type": "null"}]},
    })
    document("attempt_context", ["run_id", "run_context_sha256", "task_key", "parameters", "task_instance_id", "attempt_id", "execution_token_sha256", "retry_of", "process_role", "context_sha256"], {
        "run_id": {"type": "string", "pattern": "^run_[0-9a-f]{64}$"}, "run_context_sha256": SHA,
        "task_key": STR, "parameters": PARAMS,
        "task_instance_id": {"type": "string", "pattern": "^ti_[0-9a-f]{64}$"},
        "attempt_id": {"type": "string", "pattern": "^att_[0-9a-f]{64}$"},
        "execution_token_sha256": SHA, "retry_of": {"oneOf": [{"type": "string", "pattern": "^att_[0-9a-f]{64}$"}, {"type": "null"}]},
        "process_role": STR, "context_sha256": SHA,
    })
    document("job_ref", ["job_ref_id", "platform_id", "scheduler", "cluster_id", "scheduler_job_id", "submission_instance", "run_id", "attempt_id", "role"], {
        "job_ref_id": {"type": "string", "pattern": "^job_[0-9a-f]{64}$"},
        "platform_id": STR, "scheduler": STR, "cluster_id": STR,
        "scheduler_job_id": STR, "submission_instance": STR,
        "run_id": {"type": "string", "pattern": "^run_[0-9a-f]{64}$"},
        "attempt_id": {"type": "string", "pattern": "^att_[0-9a-f]{64}$"},
        "role": {"enum": ["primary", "auxiliary"]},
        "array_index": INT, "step_id": STR,
    })
    version_evidence = obj(["kind", "value"], {"kind": {"enum": ["content_sha256", "object_generation", "producer_version"]}, "value": STR})
    document("artifact", ["artifact_id", "artifact_version_id", "artifact_kind", "cycle", "lead_seconds", "member", "domain", "version_evidence", "location_refs", "producer_run_id", "producer_attempt_id"], {
        "artifact_id": STR, "artifact_version_id": STR, "artifact_kind": STR, "cycle": cycle,
        "lead_seconds": INT, "member": {"type": ["string", "null"]}, "domain": {"type": ["string", "null"]},
        "version_evidence": version_evidence,
        "location_refs": {"type": "array", "minItems": 1, "items": obj(["locator", "kind"], {"locator": STR, "kind": {"enum": ["path", "uri", "opaque"]}})},
        "producer_run_id": {"type": ["string", "null"]}, "producer_attempt_id": {"type": ["string", "null"]},
    })
    expected_cycle = obj(["cycle", "status", "reason"], {"cycle": cycle, "status": {"enum": ["expected", "cancelled"]}, "reason": {"type": ["string", "null"]}})
    slot = obj(["slot_id", "artifact_id", "rule_id", "rule_version", "deadline_at", "recipient"], {
        "slot_id": STR, "artifact_id": STR, "rule_id": STR, "rule_version": STR,
        "deadline_at": TIME, "recipient": STR,
    })
    document("expectation", ["plan_id", "system_id", "purpose", "deployment_id", "approved_by_ref", "cycles", "slots"], {
        "plan_id": STR, "system_id": STR, "purpose": STR, "deployment_id": STR,
        "approved_by_ref": STR, "cycles": {"type": "array", "minItems": 1, "items": expected_cycle},
        "slots": {"type": "array", "items": slot},
    })
    source = obj(["kind", "source_id", "session_id"], {
        "kind": {"enum": ["direct", "observed", "inferred"]}, "source_id": STR,
        "session_id": STR, "seq": INT, "engine_instance": STR, "boot_id": STR, "parser_version": STR,
    })
    association = obj(["run_id"], {"run_id": STR, "task_instance_id": STR, "attempt_id": STR, "job_ref_id": STR})
    evidence = obj(["evidence_id", "locator", "source", "availability"], {
        "evidence_id": STR, "locator": STR, "source": STR,
        "availability": {"enum": ["present", "expired", "unknown"]},
        "sha256": SHA, "byte_start": INT, "byte_end": INT,
        "generation": STR, "retention_until": TIME,
    })
    document("event", ["event_id", "event_type", "producer", "source", "occurred_at", "recorded_at", "time_quality", "association", "payload"], {
        "event_id": {"oneOf": [{"type": "string", "format": "uuid"}, {"type": "string", "pattern": "^evt_[0-9a-f]{64}$"}]},
        "event_type": {"type": "string", "pattern": "^[a-z][a-z0-9_]*(\\.[a-z][a-z0-9_]*)+$"},
        "producer": obj(["name", "version", "role"], {"name": STR, "version": STR, "role": STR}),
        "source": source, "occurred_at": {"oneOf": [TIME, {"type": "null"}]},
        "recorded_at": TIME,
        "time_quality": obj(["basis"], {"basis": {"enum": ["producer_clock", "source_log", "unknown"]}, "reason": STR, "raw_timezone": STR, "precision_ns": INT, "clock_error_bound_ns": INT}),
        "association": association, "payload": {"type": "object"},
        "evidence_refs": {"type": "array", "items": evidence},
    })

    catalog: dict[str, dict] = {}

    def add(name: str, roles: list[str], source_kinds: list[str], association: str, required: list[str], props: dict, *, evidence_required: bool = False) -> None:
        catalog[name] = {"roles": roles, "source_kinds": source_kinds, "association": association,
                         "evidence_required": evidence_required, "payload_schema": payload(required, props)}

    add("run.registered", ["registry"], ["direct"], "run", ["run_context_sha256", "request_sha256"], {"run_context_sha256": SHA, "request_sha256": SHA})
    add("run.finish_declared", ["lifecycle_owner"], ["direct"], "run", ["declaration_id", "scope_ref", "rule_version", "covered_attempt_ids", "covered_attempts_sha256", "reason", "result"], {"declaration_id": STR, "scope_ref": STR, "rule_version": STR, "covered_attempt_ids": {"type": "array", "uniqueItems": True, "items": STR}, "covered_attempts_sha256": SHA, "reason": STR, "result": {"enum": ["finished", "abandoned", "unknown"]}})
    add("attempt.registered", ["registry"], ["direct"], "attempt", ["attempt_context_sha256", "execution_token_sha256", "retry_of"], {"attempt_context_sha256": SHA, "execution_token_sha256": SHA, "retry_of": {"type": ["string", "null"]}})
    add("attempt.selection_declared", ["lifecycle_owner"], ["direct"], "instance", ["selection_id", "selected_attempt_id", "candidate_attempt_ids", "reason"], {"selection_id": STR, "selected_attempt_id": STR, "candidate_attempt_ids": {"type": "array", "minItems": 1, "uniqueItems": True, "items": STR}, "reason": STR, "supersedes_selection_id": STR})
    add("expectation.revision_declared", ["rule_owner"], ["direct"], "run", ["revision_id", "old_ref", "new_ref", "reason", "authorized_by_ref"], {"revision_id": STR, "old_ref": ref, "new_ref": ref, "reason": STR, "authorized_by_ref": STR})
    add("attempt.started", ["process_owner"], ["direct"], "attempt", ["invocation_id", "process_role"], {"invocation_id": STR, "process_role": STR})
    add("attempt.exited", ["process_owner"], ["direct"], "attempt", ["termination", "process_role"], {"termination": {"enum": ["exited", "signaled"]}, "exit_code": INT, "signal": INT, "process_role": STR})
    add("stage.started", ["stage_owner"], ["direct"], "attempt", ["stage_key", "interval_id"], {"stage_key": STR, "interval_id": STR})
    add("stage.ended", ["stage_owner"], ["direct"], "attempt", ["stage_key", "interval_id", "result"], {"stage_key": STR, "interval_id": STR, "result": {"enum": ["success", "failure", "unknown"]}, "duration_ns": INT})
    add("submission.requested", ["submitter"], ["direct"], "attempt", ["submission_instance", "platform_id", "scheduler", "resource_ref"], {"submission_instance": STR, "platform_id": STR, "scheduler": STR, "resource_ref": STR})
    add("submission.accepted", ["submitter", "scheduler_observer"], ["direct", "observed"], "attempt", ["submission_instance", "job_ref_id", "confirmation_basis"], {"submission_instance": STR, "job_ref_id": STR, "confirmation_basis": STR})
    add("submission.failed", ["submitter", "scheduler_observer"], ["direct", "observed"], "attempt", ["submission_instance", "outcome", "failure_stage", "error_class"], {"submission_instance": STR, "outcome": {"enum": ["not_accepted", "unknown"]}, "failure_stage": STR, "error_class": STR})
    add("engine.state_observed", ["engine_observer"], ["observed"], "run", ["engine_instance", "node_path", "raw_state", "observation_time", "source_position"], {"engine_instance": STR, "node_path": STR, "raw_state": STR, "observation_time": TIME, "source_position": STR})
    add("engine.control_observed", ["engine_observer"], ["observed"], "run", ["operation", "audit_ref"], {"operation": STR, "audit_ref": STR}, evidence_required=True)
    add("job.state_observed", ["scheduler_observer"], ["observed"], "run", ["job_ref_id", "raw_state", "normalized_state", "observation_time"], {"job_ref_id": STR, "raw_state": STR, "normalized_state": {"enum": ["queued", "running", "finished", "failed", "unknown"]}, "observation_time": TIME})
    add("data.wait_started", ["data_checker"], ["direct"], "attempt", ["wait_id", "selector"], {"wait_id": STR, "selector": STR})
    add("data.wait_ended", ["data_checker"], ["direct"], "attempt", ["wait_id", "selector", "result"], {"wait_id": STR, "selector": STR, "result": {"enum": ["ready", "timeout", "error"]}})
    add("artifact.file_observed", ["artifact_checker"], ["observed"], "run", ["artifact_version_ref", "exists", "observation_time"], {"artifact_version_ref": STR, "exists": {"type": "boolean"}, "size_bytes": INT, "mtime_at": TIME, "observation_time": TIME})
    add("artifact.stable_observed", ["artifact_checker"], ["observed"], "run", ["artifact_version_ref", "size_bytes", "sample_count", "interval_ns", "policy_version"], {"artifact_version_ref": STR, "size_bytes": {"type": "integer", "minimum": 1}, "sample_count": {"type": "integer", "minimum": 2}, "interval_ns": INT, "policy_version": STR})
    add("artifact.validation_completed", ["artifact_checker"], ["direct", "observed"], "run", ["artifact_version_ref", "rule_id", "rule_version", "check_id", "result"], {"artifact_version_ref": STR, "rule_id": STR, "rule_version": STR, "check_id": STR, "result": {"enum": ["pass", "fail", "unknown"]}}, evidence_required=True)
    add("artifact.generated", ["artifact_producer"], ["direct"], "attempt", ["artifact_version_ref", "location_ref", "version_evidence_ref"], {"artifact_version_ref": STR, "location_ref": STR, "version_evidence_ref": STR})
    add("artifact.used", ["artifact_producer"], ["direct"], "attempt", ["artifact_version_ref", "input_role", "upstream_run_id"], {"artifact_version_ref": STR, "input_role": STR, "upstream_run_id": {"type": ["string", "null"]}})
    add("artifact.published", ["publisher"], ["direct"], "attempt", ["publication_id", "artifact_version_ref", "destination", "action_result"], {"publication_id": STR, "artifact_version_ref": STR, "destination": STR, "action_result": {"enum": ["success", "failure", "unknown"]}})
    add("delivery.confirmed", ["delivery_verifier"], ["direct", "observed"], "run", ["expectation_slot", "artifact_version_ref", "rule_version", "recipient", "check_id", "confirmed_at"], {"expectation_slot": STR, "artifact_version_ref": STR, "rule_version": STR, "recipient": STR, "check_id": STR, "confirmed_at": TIME}, evidence_required=True)

    (OUT / "event-types.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    capabilities = {"contract_version": "1.0.0", "read_contracts": ["1.x"],
                    "write_contracts": ["1.0.0"], "known_event_types": sorted(catalog)}
    (OUT / "capabilities.json").write_text(json.dumps(capabilities, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
