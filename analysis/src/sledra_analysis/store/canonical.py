"""Canonical JSON bytes for the frozen 1.0 contract, independent of test helpers."""

from __future__ import annotations

import hashlib
import json
import unicodedata

CANONICAL_VERSION = "sledra-canonical-1.0.0"
MAX_DEPTH = 64


class RecordError(ValueError):
    """Invalid or over-budget input; the caller retains bytes for future quarantine."""


def _pairs(items: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise RecordError("duplicate JSON key")
        result[key] = value
    return result


def _reject_number(value: str) -> None:
    raise RecordError("floating-point and non-finite JSON values are forbidden")


def _string(value: str) -> str:
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise RecordError("unpaired Unicode surrogate")
    return unicodedata.normalize("NFC", value)


def _normalize(value: object, depth: int = 0) -> object:
    if depth > MAX_DEPTH:
        raise RecordError("JSON nesting exceeds limit")
    if isinstance(value, str):
        return _string(value)
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if not -(1 << 63) <= value < (1 << 63):
            raise RecordError("integer exceeds signed 64-bit range")
        return value
    if isinstance(value, list):
        return [_normalize(item, depth + 1) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RecordError("JSON keys must be strings")
            normalized_key = _string(key)
            if normalized_key in result:
                raise RecordError("keys collide after NFC normalization")
            result[normalized_key] = _normalize(item, depth + 1)
        return result
    raise RecordError("unsupported JSON value")


def parse_json(raw: bytes) -> object:
    try:
        # Decode explicitly: json.loads(bytes) otherwise also accepts UTF-16/32.
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
        return _normalize(value)
    except (UnicodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        if isinstance(exc, RecordError):
            raise
        raise RecordError("invalid UTF-8 JSON") from exc


def canonical_bytes(value: object) -> bytes:
    encoded = json.dumps(
        _normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return encoded.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029").encode("utf-8")


def canonical_hash(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()
