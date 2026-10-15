"""Offline 1.x envelope/payload validation and explicit unknown-semantics status."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from importlib.resources import files

from jsonschema import Draft202012Validator, FormatChecker

from sledra_analysis.store.canonical import RecordError, canonical_hash, parse_json
from sledra_analysis.store.raw import _event, _timestamp

VALIDATOR_VERSION = "sledra-event-validation-1.1.0"


@dataclass(frozen=True)
class ValidatedEvent:
    event: dict
    supported: bool


class EventValidator:
    """Validate immutable packaged contracts; never fetch schemas or guess success."""

    version = VALIDATOR_VERSION

    def __init__(self):
        root = files("sledra_analysis").joinpath("resources/contract")
        schema = json.loads(root.joinpath("event.schema.json").read_text(encoding="utf-8"))
        self.catalog = json.loads(root.joinpath("event-types.json").read_text(encoding="utf-8"))
        checker = FormatChecker()
        checker.checks("date-time")(_timestamp)
        self.envelope = Draft202012Validator(schema, format_checker=checker)
        self.payloads = {
            name: Draft202012Validator(entry["payload_schema"], format_checker=checker)
            for name, entry in self.catalog.items()
        }

    def validate(self, raw: bytes, *, event_pointer: str = "") -> ValidatedEvent:
        value = parse_json(raw)
        if event_pointer:
            if event_pointer != "/outbox_event" or not isinstance(value, dict):
                raise RecordError("invalid event pointer", "schema_invalid")
            value = value.get("outbox_event")
        if not isinstance(value, dict):
            raise RecordError("expected event object", "schema_invalid")
        version = value.get("schema_version")
        if isinstance(version, str) and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            if version.split(".")[0] != "1":
                raise RecordError("unsupported event major", "unsupported_major")
        if not self.envelope.is_valid(value):
            raise RecordError("invalid event envelope", "schema_invalid")
        try:
            value = _event(value, "")
        except RecordError as exc:
            raise RecordError("invalid event identity/time", "semantic_invalid") from exc
        quality, association = value["time_quality"], value["association"]
        if (value["occurred_at"] is None or quality["basis"] == "unknown") and not quality.get(
            "reason"
        ):
            raise RecordError("missing time quality reason", "semantic_invalid")
        if "attempt_id" in association and "task_instance_id" not in association:
            raise RecordError("attempt without task instance", "parent_mismatch")
        for evidence in value.get("evidence_refs", []):
            if (
                evidence.get("byte_end", 0) < evidence.get("byte_start", 0)
                and "byte_end" in evidence
            ):
                raise RecordError("invalid evidence range", "semantic_invalid")
        name, payload = value["event_type"], value["payload"]
        if name == "task.success":
            raise RecordError("reserved event type", "semantic_invalid")
        entry = self.catalog.get(name)
        if entry is None:
            return ValidatedEvent(value, False)
        if (
            value["producer"]["role"] not in entry["roles"]
            or value["source"]["kind"] not in entry["source_kinds"]
        ):
            raise RecordError("invalid producer authority", "unauthorized_event")
        level = entry["association"]
        if (level in ("instance", "attempt") and "task_instance_id" not in association) or (
            level == "attempt" and "attempt_id" not in association
        ):
            raise RecordError("missing association", "parent_mismatch")
        if entry["evidence_required"] and not value.get("evidence_refs"):
            raise RecordError("missing evidence", "semantic_invalid")
        if not self.payloads[name].is_valid(payload):
            raise RecordError("invalid known payload", "schema_invalid")
        if name == "attempt.exited":
            exited = payload["termination"] == "exited"
            if (
                exited
                and ("exit_code" not in payload or "signal" in payload)
                or not exited
                and ("signal" not in payload or "exit_code" in payload)
            ):
                raise RecordError("inconsistent exit fields", "semantic_invalid")
        if name == "run.finish_declared":
            ids = payload["covered_attempt_ids"]
            if ids != sorted(ids) or canonical_hash(ids) != payload["covered_attempts_sha256"]:
                raise RecordError("invalid finish coverage", "semantic_invalid")
        if (
            name == "attempt.selection_declared"
            and payload["selected_attempt_id"] not in payload["candidate_attempt_ids"]
        ):
            raise RecordError("selection outside candidate set", "semantic_invalid")
        if (
            name in ("attempt.started", "attempt.exited", "stage.started", "stage.ended")
            and value["occurred_at"] is None
        ):
            raise RecordError("missing process/stage occurrence time", "semantic_invalid")
        return ValidatedEvent(value, True)
