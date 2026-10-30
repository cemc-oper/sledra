"""Offline CE-1 reference checks. No recorder, filesystem coordination or recovery."""

from __future__ import annotations

import base64
import binascii
import hashlib
import importlib.util
import json
import posixpath
import re
from pathlib import Path

from jsonschema import Draft202012Validator

SPEC = Path(__file__).resolve().parent
ROOT = SPEC.parents[1]
_module = importlib.util.spec_from_file_location(
    "execution_v2_reference", SPEC.parent / "execution-v2/check.py"
)
legacy = importlib.util.module_from_spec(_module)
_module.loader.exec_module(legacy)
ContractError = legacy.ContractError
fail = legacy.fail
LIMITS = json.loads((SPEC / "limits.json").read_bytes())
SCHEMA = json.loads((SPEC / "contract.schema.json").read_bytes())
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
HEX = re.compile(r"[0-9a-f]{64}\Z")
KINDS = {
    "start": "execution.started",
    "context": "execution.context",
    "end": "execution.ended",
    "stage-start": "stage.started",
    "stage-end": "stage.ended",
}
RESULTS = {
    0: {
        "committed": ["ok"],
        "noop": ["same_context", "same_end"],
        "attached": ["ok"],
        "confirmed": ["ok"],
    },
    20: {
        "rejected": [
            "usage",
            "invalid_value",
            "duplicate_key",
            "limit_exceeded",
            "reserved_event_name",
            "role_denied",
            "stage_order",
            "reference_switch",
        ]
    },
    21: {"busy": ["writer_busy"]},
    22: {"closed": ["closing", "closed", "conflicting_end"]},
    23: {
        "disabled": [
            "session_disabled",
            "indeterminate",
            "control_unavailable",
            "stale_writing",
            "conflicting_context",
            "disable_publish_failed",
        ]
    },
    24: {"unknown": ["result_unknown"]},
    25: {"incompatible": ["protocol_mismatch", "installation_incompatible"]},
    26: {"unavailable": ["invalid_reference", "identity_mismatch", "owner_gone"]},
}


def bounded(kind: str, raw: bytes) -> bytes:
    if len(raw) > LIMITS[kind]:
        fail("limit_exceeded")
    return raw


def absolute(value: str, limit: int = 1024) -> str:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        fail("invalid_reference")
    if size > limit:
        fail("limit_exceeded")
    if (
        not value.startswith("/")
        or value.startswith("//")
        or posixpath.normpath(value) != value
        or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)
    ):
        fail("invalid_reference")
    return value


def reference(raw: bytes) -> str:
    bounded("reference", raw)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        fail("invalid_reference")
    if not text.startswith("sledra-ce1:"):
        fail("invalid_reference")
    path = text[len("sledra-ce1:") :]
    absolute(path, LIMITS["reference"])
    directory, filename = posixpath.split(path)
    runtime, session = posixpath.split(directory)
    absolute(runtime)
    if filename != "event-context.json" or not UUID.fullmatch(session):
        fail("invalid_reference")
    return text


def uint64(value: str, positive: bool = False) -> None:
    if not re.fullmatch(r"0|[1-9][0-9]{0,19}", value) or not int(positive) <= int(value) < 2**64:
        fail("schema_invalid")


def _decode(raw: bytes) -> object:
    try:
        return json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=legacy.pairs,
            parse_float=lambda _: fail("schema_invalid"),
            parse_constant=lambda _: fail("invalid_json"),
        )
    except (UnicodeError, ValueError, RecursionError) as exc:
        if isinstance(exc, ContractError):
            raise
        fail("invalid_json")


def _record(
    encoded: str, execution_id: str | None = None, name: str | None = None
) -> tuple[dict, bytes]:
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        fail("invalid_value")
    if base64.b64encode(raw).decode() != encoded:
        fail("invalid_value")
    event = legacy.validate("event", raw)
    if execution_id is not None and event["execution_id"] != execution_id:
        fail("identity_mismatch")
    if name is not None and event["name"] != name:
        fail("invalid_value")
    return event, raw


def _operation(
    event: dict, kind: str, auxiliary: str, event_id: str, digest: str, raw: bytes
) -> None:
    if event["event_id"] != event_id or hashlib.sha256(raw).hexdigest() != digest:
        fail("identity_mismatch")
    if kind == "event":
        if event["name"].startswith(("execution.", "stage.")):
            fail("reserved_event_name")
    elif event["name"] != KINDS[kind]:
        fail("invalid_value")
    expected = (
        event["occurred_at"]
        if kind == "start"
        else event["attributes"]["stage_id"]
        if kind == "stage-start"
        else "-"
    )
    if auxiliary != expected:
        fail("identity_mismatch")


def validate(kind: str, raw: bytes) -> dict:
    bounded(kind, raw)
    value = _decode(raw)
    validator = Draft202012Validator({"$ref": f"#/$defs/{kind}", "$defs": SCHEMA["$defs"]})
    if not validator.is_valid(value):
        fail("schema_invalid")
    if kind == "descriptor":
        for field in ("runtime_path", "root_path"):
            absolute(value[field])
        runtime, root = value["runtime_path"], value["root_path"]
        if (
            runtime == root
            or runtime.startswith(root.rstrip("/") + "/")
            or root.startswith(runtime.rstrip("/") + "/")
        ):
            fail("invalid_reference")
        legacy.validate("root", json.dumps(value["root_config"]).encode())
        legacy.timestamp(value["entry_time"])
        if not value["entry_time"].endswith("Z"):
            fail("invalid_value")
        for field in (
            "pid_namespace_inode",
            "owner_start_ticks",
            "runtime_device",
            "runtime_inode",
            "session_device",
            "session_inode",
            "lock_device",
            "lock_inode",
            "root_device",
            "root_inode",
        ):
            uint64(value[field], not field.endswith("device"))
    elif kind == "closing":
        event, event_raw = _record(value["record_base64"], name="execution.ended")
        _operation(event, "end", "-", value["event_id"], value["record_sha256"], event_raw)
    elif kind == "state":
        phase, pending = value["phase"], value["pending"]
        if phase in ("active", "closed") and value["revision"] < 2:
            fail("invalid_value")
        if (phase == "writing") != (pending is not None):
            fail("invalid_value")
        if phase == "creating" and (
            value["revision"] != 0
            or any(value[k] is not None for k in ("context", "active_stage", "end", "last_result"))
        ):
            fail("invalid_value")
        if (
            phase == "closed"
            and value["end"] is None
            or phase == "active"
            and value["end"] is not None
        ):
            fail("invalid_value")
        if (value["record_device"] is None) != (value["record_inode"] is None):
            fail("invalid_value")
        uncreated = phase == "creating" or phase == "writing" and pending["kind"] == "start"
        if value["record_inode"] is None and not uncreated and phase != "disabled":
            fail("invalid_value")
        if phase == "creating" and value["record_inode"] is not None:
            fail("invalid_value")
        for field in ("record_device", "record_inode"):
            if value[field] is not None:
                uint64(value[field], field.endswith("inode"))
        for field, name in (("context", "execution.context"), ("end", "execution.ended")):
            if value[field] is not None:
                _record(value[field], value["execution_id"], name)
        if value["active_stage"] is not None:
            stage = {
                "format_version": "2.0.0",
                "event_id": value["execution_id"],
                "execution_id": value["execution_id"],
                "occurred_at": "2026-10-30T00:00:00Z",
                "name": "stage.started",
                "attributes": {
                    "stage": value["active_stage"]["name"],
                    "stage_id": value["active_stage"]["stage_id"],
                },
            }
            legacy.validate("event", json.dumps(stage).encode())
        if pending is not None:
            event, event_raw = _record(pending["record_base64"], value["execution_id"])
            _operation(
                event,
                pending["kind"],
                pending["auxiliary"],
                pending["event_id"],
                pending["record_sha256"],
                event_raw,
            )
            if pending["target_phase"] != ("closed" if pending["kind"] == "end" else "active"):
                fail("invalid_value")
        last = value["last_result"]
        if last is not None:
            if (
                last["revision"] > value["revision"]
                or last["code"] not in RESULTS[0].get(last["result"], [])
                or last["result"] not in ("committed", "noop")
            ):
                fail("invalid_value")
    return value


def latch(kind: str, raw: bytes) -> None:
    bounded(kind, raw)
    if kind not in ("disabled", "diagnostic") or raw:
        fail("invalid_value")


def fields(kind: str, raw: bytes, count: int) -> list[str]:
    bounded(kind, raw)
    if not raw.endswith(b"\n") or any(b < 32 and b != 9 for b in raw[:-1]) or b"\x7f" in raw:
        fail("invalid_value")
    try:
        parts = raw[:-1].decode("utf-8").split("\t")
    except UnicodeError:
        fail("invalid_value")
    if len(parts) != count or any(not p for p in parts) or parts[0] != "1":
        fail("invalid_value")
    return parts


def prepared(raw: bytes) -> dict:
    _, request_id, execution_id, event_id, auxiliary, session, key, digest, record = fields(
        "prepared", raw, 9
    )
    if any(
        not UUID.fullmatch(v) for v in (request_id, execution_id, event_id)
    ) or not HEX.fullmatch(digest):
        fail("invalid_value")
    event_raw = record.encode("utf-8")
    event = legacy.validate("event", event_raw)
    if event["execution_id"] != execution_id:
        fail("identity_mismatch")
    name = event["name"]
    kind = next((k for k, n in KINDS.items() if n == name), "event")
    _operation(event, kind, auxiliary, event_id, digest, event_raw)
    if kind == "start":
        if not UUID.fullmatch(session) or not HEX.fullmatch(key) or not auxiliary.endswith("Z"):
            fail("invalid_value")
    elif session != "-" or key != "-":
        fail("role_denied")
    return event


def receipt(raw: bytes, rc: int, command: str = "commit") -> list[str]:
    parts = fields("receipt", raw, 9)
    _, result, code, request_id, event_id, revision, phase, stop, auxiliary = parts
    if code not in RESULTS.get(rc, {}).get(result, []):
        fail("invalid_value")
    if any(v != "-" and not UUID.fullmatch(v) for v in (request_id, event_id)):
        fail("invalid_value")
    if revision != "-" and (
        not re.fullmatch(r"0|[1-9][0-9]{0,18}", revision) or int(revision) >= 2**63
    ):
        fail("invalid_value")
    if phase not in (
        "off",
        "creating",
        "active",
        "writing",
        "closing",
        "closed",
        "disabled",
        "unavailable",
    ):
        fail("invalid_value")
    if stop not in ("none", "local", "requested", "quiescent"):
        fail("invalid_value")
    if result in ("committed", "confirmed", "noop") and ("-" in (request_id, event_id, revision)):
        fail("invalid_value")
    if result in ("disabled", "unknown", "incompatible", "unavailable") and stop == "none":
        fail("invalid_value")
    if stop in ("requested", "quiescent") and phase != "disabled":
        fail("invalid_value")
    if rc == 21 and (stop != "none" or phase not in ("active", "writing", "closing")):
        fail("invalid_value")
    if rc == 22 and (phase not in ("closing", "closed") or stop != "none"):
        fail("invalid_value")
    if rc == 23 and phase not in ("disabled", "unavailable"):
        fail("invalid_value")
    if result == "noop" and phase != ("closed" if code == "same_end" else "active"):
        fail("invalid_value")
    if result in ("committed", "confirmed") and phase not in (
        "active",
        "closed",
        "closing",
        "disabled",
    ):
        fail("invalid_value")
    if result == "attached" and (command != "attach" or phase != "active" or stop != "none"):
        fail("invalid_value")
    if result == "confirmed" and command != "result":
        fail("invalid_value")
    if command == "open" and result == "committed":
        reference(auxiliary.encode())
    elif auxiliary != "-":
        fail("invalid_value")
    return parts


def capabilities(raw: bytes) -> None:
    bounded("receipt", raw)
    if raw != b"CE-1\texecution-child-events\t1\n":
        fail("protocol_mismatch")


def authorize(role: str, kind: str, command: str = "commit", name: str = "business.event") -> None:
    if command not in ("commit", "open") or (command == "open" and kind != "start"):
        fail("role_denied")
    if role not in ("event", "owner") or kind not in (*KINDS, "event"):
        fail("role_denied")
    if kind == "event" and name.startswith(("execution.", "stage.")):
        fail("reserved_event_name")
    if (kind == "start" and command != "open") or role == "event" and kind != "event":
        fail("role_denied")


def binding(
    descriptor_raw: bytes, state_raw: bytes, context: bytes, closing_raw: bytes | None = None
) -> None:
    """Byte/identity relations only; never stat a path or inspect a process."""
    descriptor = validate("descriptor", descriptor_raw)
    state = validate("state", state_raw)
    ref = reference(context)
    expected = (
        "sledra-ce1:"
        + descriptor["runtime_path"]
        + "/"
        + descriptor["session_id"]
        + "/event-context.json"
    )
    if (
        ref != expected
        or descriptor["session_id"] != state["session_id"]
        or descriptor["execution_id"] != state["execution_id"]
        or hashlib.sha256(descriptor_raw).hexdigest() != state["descriptor_sha256"]
    ):
        fail("identity_mismatch")
    if closing_raw is not None:
        closing = validate("closing", closing_raw)
        if closing["session_id"] != state["session_id"]:
            fail("identity_mismatch")
        _record(closing["record_base64"], state["execution_id"], "execution.ended")


def argv_payload(args: list[str]) -> None:
    try:
        if any("\0" in arg for arg in args):
            fail("invalid_value")
        raw = b"".join(arg.encode("utf-8") + b"\0" for arg in args)
    except UnicodeError:
        fail("invalid_value")
    bounded("argv", raw)
