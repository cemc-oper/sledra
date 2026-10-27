"""Offline reference checks for the Execution 2.0.0 contract, not a runtime reader."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import math
import re
import sys
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator

SPEC = Path(__file__).resolve().parent
MAX_RECORD_BYTES = 65536
MAX_TEXT_BYTES = 4096
MAX_KEY_BYTES = 128
MAX_DEPTH = 16
MAX_ITEMS = 128
TIME = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})\Z"
)


class ContractError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def fail(code: str) -> None:
    raise ContractError(code)


def pairs(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            fail("duplicate_key")
        result[key] = value
    return result


def integer(token: str) -> int:
    if len(token.lstrip("-")) > 19:
        fail("number_out_of_range")
    value = int(token)
    if not -(2**63) <= value < 2**63:
        fail("number_out_of_range")
    return value


def decimal(token: str) -> Decimal:
    # Keep the decimal value, including >2**53 integers written with an exponent.
    # Binary64 conversion checks range only and is never used for identity.
    try:
        value = Decimal(token)
    except InvalidOperation:
        fail("number_out_of_range")
    approximate = float(value)
    if not math.isfinite(approximate) or (value != 0 and approximate == 0):
        fail("number_out_of_range")
    return value


def boundaries(value: object, depth: int = 0) -> None:
    if isinstance(value, str):
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            fail("invalid_unicode")
        if "\0" in value:
            fail("invalid_unicode")
        if len(encoded) > MAX_TEXT_BYTES:
            fail("limit_exceeded")
    elif isinstance(value, (dict, list)):
        if depth >= MAX_DEPTH or len(value) > MAX_ITEMS:
            fail("limit_exceeded")
        if isinstance(value, dict):
            for key, item in value.items():
                boundaries(key)
                if len(key.encode("utf-8")) > MAX_KEY_BYTES:
                    fail("limit_exceeded")
                boundaries(item, depth + 1)
        else:
            for item in value:
                boundaries(item, depth + 1)


def decode(raw: bytes) -> object:
    """A record excludes its one LF delimiter; reject before any schema coercion."""
    if len(raw) > MAX_RECORD_BYTES:
        fail("limit_exceeded")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_int=integer,
            parse_float=decimal,
            parse_constant=lambda _: fail("invalid_number"),
        )
    except UnicodeDecodeError:
        fail("invalid_unicode")
    except RecursionError:
        fail("limit_exceeded")
    except (json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        fail("invalid_json")
    boundaries(value)
    return value


def timestamp(value: str) -> tuple[int, int]:
    """UTC second and nanosecond; do not truncate RFC3339 to microseconds."""
    match = TIME.fullmatch(value)
    if match is None:
        fail("invalid_time")
    year, month, day, hour, minute, second = map(int, match.groups()[:6])
    fraction, offset = match.groups()[6:]
    try:
        date = dt.date(year, month, day)
    except ValueError:
        fail("invalid_time")
    if hour > 23 or minute > 59 or second > 59 or offset == "-00:00":
        fail("invalid_time")
    adjustment = 0
    if offset != "Z":
        oh, om = int(offset[1:3]), int(offset[4:6])
        if oh > 23 or om > 59:
            fail("invalid_time")
        adjustment = (oh * 60 + om) * 60 * (1 if offset[0] == "+" else -1)
    seconds = date.toordinal() * 86400 + hour * 3600 + minute * 60 + second - adjustment
    return seconds, int((fraction or "").ljust(9, "0"))


@lru_cache(maxsize=3)
def validator(kind: str) -> Draft202012Validator:
    schema = json.loads((SPEC / "contract.schema.json").read_bytes())
    Draft202012Validator.check_schema(schema)
    selected = {"$defs": schema["$defs"], "$ref": f"#/$defs/{kind}"}
    return Draft202012Validator(selected)


def validate(kind: str, raw: bytes) -> dict:
    if kind not in ("root", "run", "event"):
        fail("unknown_kind")
    value = decode(raw)
    if kind == "event" and (b"\r" in raw or b"\n" in raw):
        fail("invalid_json")
    if isinstance(value, dict) and value.get("format_version") != "2.0.0":
        fail("unsupported_version")
    if not validator(kind).is_valid(value):
        fail("schema_invalid")
    if kind == "root":
        offset = value["partition_timezone"]
        hour, minute = int(offset[1:3]), int(offset[4:6])
        if minute > 59 or hour > 14 or (hour == 14 and minute) or offset == "-00:00":
            fail("invalid_timezone")
    elif kind == "run":
        timestamp(value["created_at"])
        if value["cycle"] is not None:
            timestamp(value["cycle"])
    else:
        timestamp(value["occurred_at"])
        attrs = value["attributes"]
        if value["name"] == "execution.context" and attrs["cycle"] is not None:
            timestamp(attrs["cycle"])
    return value


def equivalent(left: object, right: object) -> bool:
    """Only JSON numbers share numeric equality; bool is never an integer."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, Decimal)) and isinstance(right, (int, Decimal)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            equivalent(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(equivalent(a, b) for a, b in zip(left, right))
    return left == right


def comparable(record: dict, kind: str) -> dict:
    result = dict(record)
    key = "created_at" if kind == "run" else "occurred_at"
    result[key] = timestamp(result[key])
    if kind == "run" and result["cycle"] is not None:
        result["cycle"] = timestamp(result["cycle"])
    if kind == "event" and result["name"] == "execution.context":
        result["attributes"] = dict(result["attributes"])
        if result["attributes"]["cycle"] is not None:
            result["attributes"]["cycle"] = timestamp(result["attributes"]["cycle"])
    return result


def stream_conflicts(raw: bytes) -> list[str]:
    """Fixture oracle only: no scans, runtime lifecycle, grouping or database."""
    if not raw or not raw.endswith(b"\n"):
        fail("incomplete_tail")
    seen = {}
    context = ended = execution_id = None
    conflicts = set()
    for line in raw.split(b"\n")[:-1]:
        record = validate("event", line)
        if execution_id is not None and execution_id != record["execution_id"]:
            fail("execution_mismatch")
        execution_id = record["execution_id"]
        value = comparable(record, "event")
        event_id = record["event_id"]
        if event_id in seen:
            if not equivalent(seen[event_id], value):
                conflicts.add("conflicting_event_id")
        else:
            seen[event_id] = value
        attrs = value["attributes"]
        if record["name"] == "execution.context":
            if context is not None and not equivalent(context, attrs):
                conflicts.add("conflicting_context")
            if context is None:
                context = attrs
        if record["name"] == "execution.ended":
            if ended is not None and not equivalent(ended, attrs):
                conflicts.add("conflicting_end")
            if ended is None:
                ended = attrs
    return sorted(conflicts)


def run_conflicts(records: list[bytes]) -> list[str]:
    seen = {}
    conflicts = set()
    for raw in records:
        record = validate("run", raw)
        value = comparable(record, "run")
        run_id = record["run_id"]
        if run_id in seen and not equivalent(seen[run_id], value):
            conflicts.add("conflicting_run_id")
        seen[run_id] = value
    return sorted(conflicts)


def partition_date(value: str, root: dict) -> str:
    seconds, _ = timestamp(value)
    zone = root["partition_timezone"]
    offset = (int(zone[1:3]) * 60 + int(zone[4:6])) * 60
    seconds += offset * (1 if zone[0] == "+" else -1)
    day = dt.date.fromordinal(seconds // 86400)
    return f"{day.year:04d}{day.month:02d}{day.day:02d}"


def execution_path(execution_id: str, entry_time: str, root: dict) -> str:
    """Pure layout vector helper, never creates files or locates a live root."""
    date = partition_date(entry_time, root)
    bucket = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[0]
    return f"executions/{date}/{bucket}/{execution_id}.jsonl"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=("root", "run", "event", "stream"))
    parser.add_argument("file", type=Path)
    args = parser.parse_args()
    try:
        if args.kind == "stream":
            conflicts = stream_conflicts(args.file.read_bytes())
        else:
            # Read a bounded record even when a caller supplies an oversized file.
            with args.file.open("rb") as source:
                validate(args.kind, source.read(MAX_RECORD_BYTES + 1))
            conflicts = []
        print(json.dumps({"ok": True, "conflicts": conflicts}, sort_keys=True))
    except (OSError, ContractError) as exc:
        code = exc.code if isinstance(exc, ContractError) else "input_unavailable"
        print(json.dumps({"ok": False, "code": code}), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
