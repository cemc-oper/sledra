"""Offline 1.0 contract test helper; no recorder or analyzer dependency."""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import sys
import unicodedata
import uuid
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


SPEC = Path(__file__).parent / "1.0.0"
KINDS = ("definition_manifest", "run_context", "attempt_context", "job_ref", "artifact", "expectation", "event")
TIME_KEYS = {"cycle_time", "occurred_at", "recorded_at", "deadline_at", "observation_time", "confirmed_at", "mtime_at", "retention_until"}
TIME_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$")


class ContractError(ValueError):
    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code


def fail(code: str, detail: str) -> None:
    raise ContractError(code, detail)


def pairs(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            fail("duplicate_key", key)
        result[key] = value
    return result


def read_json(path: Path) -> object:
    try:
        value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda item: fail("schema_invalid", item))
        reject_surrogates(value)
        return value
    except UnicodeDecodeError as exc:
        fail("schema_invalid", f"invalid UTF-8: {exc}")


def reject_surrogates(value: object) -> None:
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            fail("schema_invalid", "unpaired Unicode surrogate")
    elif isinstance(value, list):
        for item in value:
            reject_surrogates(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            reject_surrogates(key)
            reject_surrogates(item)


def normalized(value: object) -> object:
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        if not -(1 << 63) <= value <= (1 << 63) - 1:
            fail("semantic_invalid", "integer exceeds signed 64-bit range")
        return value
    if isinstance(value, float):
        fail("semantic_invalid", "floating-point values are forbidden")
    if isinstance(value, list):
        return [normalized(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            nk = unicodedata.normalize("NFC", key)
            if nk in result:
                fail("semantic_invalid", "keys collide after NFC normalization")
            result[nk] = normalized(item)
        return result
    fail("semantic_invalid", "unsupported JSON value")


def canonical(value: object) -> bytes:
    encoded = json.dumps(normalized(value), ensure_ascii=False, sort_keys=True,
                         separators=(",", ":"), allow_nan=False)
    return encoded.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029").encode("utf-8")


def sha(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def length_digest(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        data = unicodedata.normalize("NFC", part).encode("utf-8")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def task_instance_id(run_id: str, task_key: str, parameters: dict) -> str:
    return "ti_" + length_digest(run_id, task_key, canonical(parameters).decode("utf-8"))


def registry_id(registry: str, kind: str, parent: str, token: str) -> str:
    prefix = {"run": "run_", "attempt": "att_"}[kind]
    return prefix + length_digest(registry, kind, parent, token)


def content_sha(doc: dict, excluded: tuple[str, ...]) -> str:
    return sha({key: value for key, value in doc.items() if key not in excluded})


def schemas(spec: Path = SPEC) -> dict:
    result = {}
    for kind in KINDS:
        schema = read_json(spec / f"{kind}.schema.json")
        Draft202012Validator.check_schema(schema)
        result[kind] = Draft202012Validator(schema, format_checker=FormatChecker())
    return result


def check_structure(doc: dict, kind: str, validators: dict) -> None:
    if not isinstance(doc, dict):
        fail("schema_invalid", f"{kind} is not an object")
    version = doc.get("schema_version")
    if isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) and not version.startswith("1."):
        fail("unsupported_major", version)
    errors = list(validators[kind].iter_errors(doc))
    if errors:
        fail("schema_invalid", f"{kind}: {errors[0].message}")
    check_timestamps(doc)


def check_timestamps(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in ("extensions", "payload"):
                continue
            if key in TIME_KEYS and child is not None:
                if not isinstance(child, str) or not TIME_PATTERN.fullmatch(child):
                    fail("schema_invalid", f"timestamp needs RFC3339 offset: {key}")
                if child[-1] != "Z" and (int(child[-5:-3]) > 23 or int(child[-2:]) > 59):
                    fail("schema_invalid", f"invalid timestamp offset: {key}")
                try:
                    parsed = datetime.datetime.fromisoformat(child.replace("Z", "+00:00"))
                except ValueError:
                    fail("schema_invalid", f"invalid timestamp: {key}")
                if parsed.utcoffset() is None:
                    fail("schema_invalid", f"timestamp needs offset: {key}")
            check_timestamps(child)
    elif isinstance(value, list):
        for child in value:
            check_timestamps(child)


def check_parameters(values: dict, task: dict) -> dict:
    declared = task["parameters"]
    if set(values) - set(declared):
        fail("unknown_parameter", str(sorted(set(values) - set(declared))))
    expanded = dict(values)
    for key, rule in declared.items():
        if key not in expanded:
            if "default" in rule:
                expanded[key] = rule["default"]
            elif rule["required"]:
                fail("missing_parameter", key)
        if key not in expanded:
            continue
        value = expanded[key]
        typ = rule["type"]
        if (typ == "string" and not isinstance(value, str)) or (typ == "boolean" and not isinstance(value, bool)) or (typ == "integer" and (type(value) is not int or not -(1 << 63) <= value <= (1 << 63) - 1)):
            fail("semantic_invalid", f"wrong parameter type: {key}")
        if "enum" in rule and normalized(value) not in normalized(rule["enum"]):
            fail("semantic_invalid", f"value outside enum: {key}")
    return normalized(expanded)


def validate_bundle(bundle: dict, spec: Path = SPEC) -> dict:
    validators = schemas(spec)
    catalog = read_json(spec / "event-types.json")
    payload_validators = {}
    for event_type, entry in catalog.items():
        Draft202012Validator.check_schema(entry["payload_schema"])
        payload_validators[event_type] = Draft202012Validator(entry["payload_schema"], format_checker=FormatChecker())
    for kind in KINDS[:-1]:
        check_structure(bundle[kind], kind, validators)
    manifest, run, attempt = (bundle[key] for key in ("definition_manifest", "run_context", "attempt_context"))
    job, artifact, expectation = bundle["job_ref"], bundle["artifact"], bundle["expectation"]
    if manifest["content_sha256"] != content_sha(manifest, ("content_sha256", "definition_id")):
        fail("semantic_invalid", "manifest content hash")
    if manifest["definition_id"] != "def_" + manifest["content_sha256"]:
        fail("semantic_invalid", "definition ID")
    tasks = {task["task_key"]: task for task in manifest["tasks"]}
    if len(tasks) != len(manifest["tasks"]):
        fail("semantic_invalid", "duplicate task key")
    stages = set(manifest["stage_definitions"])
    for task in tasks.values():
        if task["stage_key"] not in stages or any(dep not in tasks for dep in task["depends_on"]):
            fail("semantic_invalid", "task stage/dependency missing")
        for key, rule in task["parameters"].items():
            if "default" in rule:
                check_parameters({key: rule["default"]}, {"parameters": {key: rule}})
            for option in rule.get("enum", []):
                check_parameters({key: option}, {"parameters": {key: rule}})
    if run["definition_ref"] != {"id": manifest["definition_id"], "sha256": manifest["content_sha256"]}:
        fail("parent_mismatch", "definition reference")
    scope = set()
    for entry in run["scope"]:
        task = tasks.get(entry["task_key"])
        if task is None:
            fail("parent_mismatch", "unknown task in scope")
        expanded = check_parameters(entry["parameters"], task)
        if canonical(expanded) != canonical(entry["parameters"]):
            fail("missing_parameter", "scope defaults must be expanded")
        key = (entry["task_key"], canonical(expanded))
        if key in scope:
            fail("semantic_invalid", "duplicate task instance in scope")
        scope.add(key)
    if run["context_sha256"] != content_sha(run, ("context_sha256",)):
        fail("semantic_invalid", "run context hash")
    if expectation["plan_id"] != "exp_" + content_sha(expectation, ("plan_id",)):
        fail("semantic_invalid", "expectation ID")
    expected_ref = {"id": expectation["plan_id"], "sha256": content_sha(expectation, ("plan_id",))}
    if run["expectation_ref"] is not None and run["expectation_ref"] != expected_ref:
        fail("parent_mismatch", "expectation reference")
    if (run["system_id"], run["purpose"], run["deployment_id"]) != (expectation["system_id"], expectation["purpose"], expectation["deployment_id"]):
        fail("parent_mismatch", "expectation scope")
    if not any(item["cycle"] == run["cycle"] and item["status"] == "expected" for item in expectation["cycles"]):
        fail("parent_mismatch", "run cycle absent from expectation")
    cycle_keys = set()
    for item in expectation["cycles"]:
        if item["status"] == "cancelled" and not item["reason"]:
            fail("semantic_invalid", "cancelled cycle needs reason")
        key = canonical(item["cycle"])
        if key in cycle_keys:
            fail("semantic_invalid", "duplicate expected cycle")
        cycle_keys.add(key)
    slots = {item["slot_id"]: item for item in expectation["slots"]}
    if len(slots) != len(expectation["slots"]):
        fail("semantic_invalid", "duplicate expectation slot")
    if attempt["run_id"] != run["run_id"] or attempt["run_context_sha256"] != run["context_sha256"]:
        fail("parent_mismatch", "attempt parent")
    if attempt["task_key"] not in tasks:
        fail("parent_mismatch", "attempt task")
    expanded = check_parameters(attempt["parameters"], tasks[attempt["task_key"]])
    if canonical(expanded) != canonical(attempt["parameters"]):
        fail("missing_parameter", "attempt defaults must be expanded")
    if (attempt["task_key"], canonical(expanded)) not in scope:
        fail("parent_mismatch", "attempt outside run scope")
    if attempt["task_instance_id"] != task_instance_id(run["run_id"], attempt["task_key"], expanded):
        fail("parent_mismatch", "task instance ID")
    if attempt["retry_of"] == attempt["attempt_id"]:
        fail("semantic_invalid", "self retry")
    if attempt["context_sha256"] != content_sha(attempt, ("context_sha256",)):
        fail("semantic_invalid", "attempt context hash")
    if (job["run_id"], job["attempt_id"], job["platform_id"]) != (run["run_id"], attempt["attempt_id"], run["platform_id"]):
        fail("parent_mismatch", "job parent/platform")
    expected_job_id = "job_" + length_digest(job["platform_id"], job["scheduler"], job["cluster_id"],
                                              job["scheduler_job_id"], job["submission_instance"])
    if job["job_ref_id"] != expected_job_id:
        fail("semantic_invalid", "job composite identity")
    if artifact["cycle"] != run["cycle"] or artifact["producer_run_id"] not in (None, run["run_id"]) or artifact["producer_attempt_id"] not in (None, attempt["attempt_id"]):
        fail("parent_mismatch", "artifact parent/cycle")
    if artifact["producer_attempt_id"] is not None and artifact["producer_run_id"] is None:
        fail("parent_mismatch", "artifact attempt without run")
    if artifact["version_evidence"]["kind"] == "content_sha256" and not re.fullmatch(r"[0-9a-f]{64}", artifact["version_evidence"]["value"]):
        fail("semantic_invalid", "invalid artifact content hash")
    if artifact["artifact_version_id"] != "av_" + content_sha(artifact, ("artifact_version_id",)):
        fail("semantic_invalid", "artifact version ID")
    seen = {}
    opaque = []
    opaque_events = []
    for event in bundle["events"]:
        check_structure(event, "event", validators)
        event_id = event["event_id"]
        encoded = canonical(event)
        if event_id in seen:
            if seen[event_id] != encoded:
                fail("conflicting_event_id", event_id)
            continue
        seen[event_id] = encoded
        if not event_id.startswith("evt_"):
            try:
                parsed_id = uuid.UUID(event_id)
            except ValueError:
                fail("semantic_invalid", "ordinary event ID must be UUIDv4")
            if parsed_id.version != 4 or str(parsed_id) != event_id:
                fail("semantic_invalid", "ordinary event ID must be canonical UUIDv4")
        assoc = event["association"]
        if assoc["run_id"] != run["run_id"]:
            fail("parent_mismatch", "event run")
        if "task_instance_id" in assoc and assoc["task_instance_id"] != attempt["task_instance_id"]:
            fail("parent_mismatch", "event task instance")
        if "attempt_id" in assoc and assoc["attempt_id"] != attempt["attempt_id"]:
            fail("parent_mismatch", "event attempt")
        if "attempt_id" in assoc and "task_instance_id" not in assoc:
            fail("parent_mismatch", "event attempt without instance")
        if "job_ref_id" in assoc and assoc["job_ref_id"] != job["job_ref_id"]:
            fail("parent_mismatch", "event job reference")
        if event["occurred_at"] is None and "reason" not in event["time_quality"]:
            fail("semantic_invalid", "unknown occurred_at needs reason")
        if event["time_quality"]["basis"] == "unknown" and "reason" not in event["time_quality"]:
            fail("semantic_invalid", "unknown time basis needs reason")
        if event["event_type"] == "task.success":
            fail("semantic_invalid", "task.success is reserved and forbidden")
        entry = catalog.get(event["event_type"])
        if entry is None:
            opaque.append(event_id)
            opaque_events.append(event)
            continue
        if event["producer"]["role"] not in entry["roles"] or event["source"]["kind"] not in entry["source_kinds"]:
            fail("unauthorized_event", event["event_type"])
        level = entry["association"]
        if level in ("instance", "attempt") and "task_instance_id" not in assoc:
            fail("parent_mismatch", "event lacks task instance")
        if level == "attempt" and "attempt_id" not in assoc:
            fail("parent_mismatch", "event lacks attempt")
        if entry["evidence_required"] and not event.get("evidence_refs"):
            fail("semantic_invalid", "evidence required")
        for evidence in event.get("evidence_refs", []):
            if "byte_start" in evidence and "byte_end" in evidence and evidence["byte_end"] < evidence["byte_start"]:
                fail("semantic_invalid", "evidence byte range")
        errors = list(payload_validators[event["event_type"]].iter_errors(event["payload"]))
        if errors:
            fail("schema_invalid", f"{event['event_type']}: {errors[0].message}")
        p = event["payload"]
        check_timestamps(p)
        if event["event_type"] == "attempt.exited":
            if (p["termination"] == "exited" and ("exit_code" not in p or "signal" in p)) or (p["termination"] == "signaled" and ("signal" not in p or "exit_code" in p)):
                fail("semantic_invalid", "termination/code/signal mismatch")
        if event["event_type"] in ("attempt.started", "attempt.exited", "stage.started", "stage.ended") and event["occurred_at"] is None:
            fail("semantic_invalid", "direct process/stage boundary needs occurred_at")
        if event["event_type"] == "run.registered" and (p["run_context_sha256"] != run["context_sha256"] or p["request_sha256"] != run["request_sha256"]):
            fail("parent_mismatch", "run registration payload")
        if event["event_type"] == "attempt.registered" and (p["attempt_context_sha256"] != attempt["context_sha256"] or p["execution_token_sha256"] != attempt["execution_token_sha256"] or p["retry_of"] != attempt["retry_of"]):
            fail("parent_mismatch", "attempt registration payload")
        if event["event_type"] == "run.finish_declared":
            ids = p["covered_attempt_ids"]
            if ids != sorted(ids) or p["covered_attempts_sha256"] != sha(ids):
                fail("semantic_invalid", "covered attempt set/hash")
        if event["event_type"] == "attempt.selection_declared" and p["selected_attempt_id"] not in p["candidate_attempt_ids"]:
            fail("semantic_invalid", "selected attempt outside candidates")
        if event["event_type"] in ("submission.accepted", "job.state_observed") and (p["job_ref_id"] != job["job_ref_id"] or assoc.get("job_ref_id") != job["job_ref_id"]):
            fail("parent_mismatch", "event payload job reference")
        if event["event_type"] == "submission.accepted" and p["submission_instance"] != job["submission_instance"]:
            fail("parent_mismatch", "submission generation")
        if event["event_type"] == "artifact.validation_completed" and p["artifact_version_ref"] != artifact["artifact_version_id"]:
            fail("parent_mismatch", "validation artifact version")
        if event["event_type"] == "delivery.confirmed":
            slot = slots.get(p["expectation_slot"])
            if (slot is None or p["artifact_version_ref"] != artifact["artifact_version_id"]
                    or slot["artifact_id"] != artifact["artifact_id"]
                    or p["recipient"] != slot["recipient"] or p["rule_version"] != slot["rule_version"]):
                fail("parent_mismatch", "delivery expectation/artifact binding")
    return {"known": len(seen) - len(opaque), "opaque": opaque, "opaque_events": opaque_events,
            "event_count": len(seen), "bundle_canonical_sha256": sha(bundle)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a local Sledra contract bundle")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--spec-dir", type=Path, default=SPEC)
    args = parser.parse_args()
    try:
        result = validate_bundle(read_json(args.bundle), args.spec_dir)
    except (ContractError, KeyError) as exc:
        code = exc.code if isinstance(exc, ContractError) else "schema_invalid"
        print(json.dumps({"ok": False, "code": code, "detail": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
