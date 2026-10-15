"""Packaged parent contracts and content/instance identity checks, all offline."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from dataclasses import dataclass
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker

from sledra_analysis.store.canonical import RecordError, canonical_bytes, canonical_hash, parse_json
from sledra_analysis.store.raw import _timestamp

KINDS = {
    "definition_manifest": "definition_id",
    "run_context": "run_id",
    "attempt_context": "attempt_id",
    "job_ref": "job_ref_id",
}


@dataclass(frozen=True)
class DocumentRecord:
    raw_bytes: bytes
    locator: str
    collected_at: str
    pointer: str = ""


def length_digest(*parts: str) -> str:
    digest = hashlib.sha256()
    for part in parts:
        data = unicodedata.normalize("NFC", part).encode()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def instance_id(run_id: str, task_key: str, parameters: dict) -> str:
    return "ti_" + length_digest(run_id, task_key, canonical_bytes(parameters).decode())


def content_hash(value: dict, *excluded: str) -> str:
    return canonical_hash({k: v for k, v in value.items() if k not in excluded})


def check_parameters(values: dict, task: dict) -> None:
    rules = task["parameters"]
    if set(values) - set(rules):
        raise RecordError("undeclared parameters", "unknown_parameter")
    for key, rule in rules.items():
        if key not in values:
            if rule["required"] or "default" in rule:
                raise RecordError("parameters must be expanded", "missing_parameter")
            continue
        value = values[key]
        if type(value) is not {"integer": int, "boolean": bool, "string": str}[rule["type"]]:
            raise RecordError("parameter type differs", "semantic_invalid")
        if "enum" in rule and canonical_bytes(value) not in [
            canonical_bytes(v) for v in rule["enum"]
        ]:
            raise RecordError("parameter outside enumeration", "semantic_invalid")


class DocumentValidator:
    def __init__(self):
        root = files("sledra_analysis").joinpath("resources/contract")
        checker = FormatChecker()
        checker.checks("date-time")(_timestamp)
        self.schemas = {
            kind: Draft202012Validator(
                json.loads(root.joinpath(f"{kind}.schema.json").read_bytes()),
                format_checker=checker,
            )
            for kind in KINDS
        }

    def validate(self, raw: bytes, pointer: str = "") -> dict:
        value = parse_json(raw)
        if pointer:
            if pointer != "/context" or not isinstance(value, dict):
                raise RecordError("unsupported document pointer", "schema_invalid")
            value = value.get("context")
        if not isinstance(value, dict) or value.get("kind") not in KINDS:
            raise RecordError("unsupported parent document", "schema_invalid")
        kind = value["kind"]
        if not self.schemas[kind].is_valid(value):
            raise RecordError("invalid parent document", "schema_invalid")
        # canonical_bytes also rejects floats and normalization collisions in extensions.
        canonical_bytes(value)
        if kind == "definition_manifest":
            digest = content_hash(value, "content_sha256", "definition_id")
            if value["content_sha256"] != digest or value["definition_id"] != "def_" + digest:
                raise RecordError("definition content identity differs", "semantic_invalid")
            tasks = {t["task_key"]: t for t in value["tasks"]}
            if len(tasks) != len(value["tasks"]):
                raise RecordError("duplicate task key", "semantic_invalid")
            for task in tasks.values():
                if task["stage_key"] not in value["stage_definitions"] or any(
                    d not in tasks for d in task["depends_on"]
                ):
                    raise RecordError("unknown task dependency or stage", "parent_mismatch")
                for name, rule in task["parameters"].items():
                    for option in ([rule["default"]] if "default" in rule else []) + rule.get(
                        "enum", []
                    ):
                        check_parameters({name: option}, {"parameters": {name: rule}})
        elif kind in ("run_context", "attempt_context"):
            if value["context_sha256"] != content_hash(value, "context_sha256"):
                raise RecordError("context content hash differs", "semantic_invalid")
            if kind == "attempt_context":
                if value["task_instance_id"] != instance_id(
                    value["run_id"], value["task_key"], value["parameters"]
                ):
                    raise RecordError("task instance identity differs", "parent_mismatch")
                if value["retry_of"] == value["attempt_id"]:
                    raise RecordError("self retry", "semantic_invalid")
        elif kind == "job_ref":
            expected = "job_" + length_digest(
                *(
                    value[k]
                    for k in (
                        "platform_id",
                        "scheduler",
                        "cluster_id",
                        "scheduler_job_id",
                        "submission_instance",
                    )
                )
            )
            if value["job_ref_id"] != expected:
                raise RecordError("job composite identity differs", "semantic_invalid")
        return parse_json(canonical_bytes(value))
