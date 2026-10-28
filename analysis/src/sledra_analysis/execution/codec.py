"""Bounded Execution 2.0.0 decoding, independent of legacy ingestion/storage."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from importlib.resources import files

from jsonschema import Draft202012Validator, SchemaError

FORMAT_VERSION = "2.0.0"
LAYOUT_VERSION = 2
MAX_RECORD_BYTES = 65536
MAX_TEXT_BYTES = 4096
MAX_KEY_BYTES = 128
MAX_DEPTH = 16
MAX_ITEMS = 128
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
TIME = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})"
    r"(?:\.([0-9]{1,9}))?(Z|[+-][0-9]{2}:[0-9]{2})\Z"
)


class ContractError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _fail(code: str):
    raise ContractError(code)


def _pairs(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            _fail("duplicate_key")
        result[key] = value
    return result


def _integer(token: str) -> int:
    if len(token.lstrip("-")) > 19:
        _fail("number_out_of_range")
    value = int(token)
    if not -(2**63) <= value < 2**63:
        _fail("number_out_of_range")
    return value


def _decimal(token: str) -> Decimal:
    try:
        value = Decimal(token)
    except InvalidOperation:
        # A valid zero token remains exactly zero even when its exponent exceeds
        # Decimal's representation range. Go's binary64 range check accepts it.
        mantissa = re.split("[eE]", token, maxsplit=1)[0]
        if not any(c in "123456789" for c in mantissa):
            return Decimal("-0" if token.startswith("-") else "0")
        _fail("number_out_of_range")
    approximate = float(value)
    if not math.isfinite(approximate) or (value != 0 and approximate == 0):
        _fail("number_out_of_range")
    return value


def _boundaries(value: object, depth: int = 0) -> None:
    if isinstance(value, str):
        try:
            encoded = value.encode("utf-8")
        except UnicodeEncodeError:
            _fail("invalid_unicode")
        if "\0" in value:
            _fail("invalid_unicode")
        if len(encoded) > MAX_TEXT_BYTES:
            _fail("limit_exceeded")
    elif isinstance(value, (dict, list)):
        if depth >= MAX_DEPTH or len(value) > MAX_ITEMS:
            _fail("limit_exceeded")
        if isinstance(value, dict):
            for key, child in value.items():
                _boundaries(key)
                if len(key.encode("utf-8")) > MAX_KEY_BYTES:
                    _fail("limit_exceeded")
                _boundaries(child, depth + 1)
        else:
            for child in value:
                _boundaries(child, depth + 1)


def decode(raw: bytes) -> object:
    """Decode one object/value excluding LF, preserving exact decimal values."""
    if len(raw) > MAX_RECORD_BYTES:
        _fail("limit_exceeded")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_int=_integer,
            parse_float=_decimal,
            parse_constant=lambda _: _fail("invalid_number"),
        )
    except UnicodeDecodeError:
        _fail("invalid_unicode")
    except RecursionError:
        _fail("limit_exceeded")
    except (json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, ContractError):
            raise
        _fail("invalid_json")
    _boundaries(value)
    return value


def timestamp(value: str) -> tuple[int, int]:
    """UTC seconds since ordinal day zero and nanoseconds, without float clocks."""
    match = TIME.fullmatch(value)
    if match is None:
        _fail("invalid_time")
    year, month, day, hour, minute, second = map(int, match.groups()[:6])
    fraction, offset = match.groups()[6:]
    try:
        date = dt.date(year, month, day)
    except ValueError:
        _fail("invalid_time")
    if hour > 23 or minute > 59 or second > 59 or offset == "-00:00":
        _fail("invalid_time")
    adjustment = 0
    if offset != "Z":
        oh, om = int(offset[1:3]), int(offset[4:6])
        if oh > 23 or om > 59:
            _fail("invalid_time")
        adjustment = (oh * 60 + om) * 60 * (1 if offset[0] == "+" else -1)
    seconds = date.toordinal() * 86400 + hour * 3600 + minute * 60 + second - adjustment
    return seconds, int((fraction or "").ljust(9, "0"))


@lru_cache(maxsize=3)
def _validator(kind: str) -> Draft202012Validator:
    try:
        schema = json.loads(
            files("sledra_analysis")
            .joinpath("resources/execution_v2/contract.schema.json")
            .read_bytes()
        )
        Draft202012Validator.check_schema(schema)
    except (OSError, ValueError, SchemaError):
        _fail("contract_unavailable")
    return Draft202012Validator({"$defs": schema["$defs"], "$ref": f"#/$defs/{kind}"})


def validate(kind: str, raw: bytes) -> dict:
    if kind not in ("root", "run", "event"):
        _fail("unknown_kind")
    value = decode(raw)
    if kind == "event" and (b"\r" in raw or b"\n" in raw):
        _fail("invalid_json")
    if isinstance(value, dict) and value.get("format_version") != FORMAT_VERSION:
        _fail("unsupported_version")
    if isinstance(value, dict):
        if kind == "root" and type(value.get("layout_version")) is not int:
            _fail("schema_invalid")
        if kind == "event" and value.get("name") in ("execution.ended", "stage.ended"):
            attrs = value.get("attributes")
            if isinstance(attrs, dict) and any(
                key in attrs and type(attrs[key]) is not int
                for key in ("exit_code", "signal", "duration_ns")
            ):
                _fail("schema_invalid")
    if not _validator(kind).is_valid(value):
        _fail("schema_invalid")
    if kind == "root":
        offset = value["partition_timezone"]
        hour, minute = int(offset[1:3]), int(offset[4:6])
        if minute > 59 or hour > 14 or (hour == 14 and minute) or offset == "-00:00":
            _fail("invalid_timezone")
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
    """Exact decoded JSON equality; booleans are distinct, Unicode is not NFC."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, Decimal)) and isinstance(right, (int, Decimal)):
        return left == right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(equivalent(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(equivalent(a, b) for a, b in zip(left, right))
    return left == right


def comparable(record: dict, kind: str) -> dict:
    """Normalize only contract-defined time fields; never produce identity hashes."""
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


def partition_date(value: str, root: dict) -> str:
    seconds, _ = timestamp(value)
    zone = root["partition_timezone"]
    seconds += (int(zone[1:3]) * 60 + int(zone[4:6])) * 60 * (1 if zone[0] == "+" else -1)
    try:
        day = dt.date.fromordinal(seconds // 86400)
    except ValueError:
        _fail("invalid_time")
    return f"{day.year:04d}{day.month:02d}{day.day:02d}"


def execution_path(execution_id: str, entry_time: str, root: dict) -> str:
    if UUID.fullmatch(execution_id) is None:
        _fail("schema_invalid")
    bucket = hashlib.sha256(execution_id.encode("utf-8")).hexdigest()[0]
    return f"executions/{partition_date(entry_time, root)}/{bucket}/{execution_id}.jsonl"
