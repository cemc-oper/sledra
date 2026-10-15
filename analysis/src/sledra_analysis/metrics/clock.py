"""Integer nanosecond arithmetic; an RFC3339 offset is not clock synchronization."""

from __future__ import annotations

import re
from datetime import datetime, timezone

PATTERN = re.compile(
    r"(\d{4}-\d{2}-\d{2})[Tt](\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?([Zz]|[+-]\d{2}:\d{2})"
)
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
LIMIT = (1 << 63) - 1


def utc_ns(value: str) -> int:
    """Reject unknown offsets, leap seconds and precision we cannot preserve."""
    match = PATTERN.fullmatch(value) if isinstance(value, str) else None
    if match is None or match[4] == "-00:00":
        raise ValueError("time requires a known RFC3339 offset and at most nine fractional digits")
    if match[4].upper() != "Z" and (int(match[4][1:3]) > 23 or int(match[4][4:6]) > 59):
        raise ValueError("invalid RFC3339 offset")
    stamp = datetime.fromisoformat(match[1] + "T" + match[2] + match[4].upper())
    delta = stamp.astimezone(timezone.utc) - EPOCH
    return (delta.days * 86400 + delta.seconds) * 10**9 + int((match[3] or "").ljust(9, "0"))


def wall_interval(start: dict, end: dict) -> dict:
    """Keep nominal UTC delta separate from a proven elapsed-time interval."""
    try:
        delta = utc_ns(end["occurred_at"]) - utc_ns(start["occurred_at"])
    except (ValueError, TypeError, KeyError, OverflowError):
        return {"quality": "unavailable", "reason": "unknown_time_or_timezone"}
    if abs(delta) > LIMIT:
        return {"quality": "unavailable", "reason": "duration_overflow"}
    if delta < 0:
        return {"quality": "conflict", "reason": "negative_wall_duration", "nominal_ns": delta}
    qualities = [e.get("time_quality", {}) for e in (start, end)]
    if any(
        q.get("basis") == "unknown"
        or any(
            type(q.get(k)) is not int or q[k] < 0 for k in ("clock_error_bound_ns", "precision_ns")
        )
        for q in qualities
    ):
        return {"quality": "uncertain", "reason": "clock_quality_unknown", "nominal_ns": delta}
    error = sum(q["clock_error_bound_ns"] + q["precision_ns"] for q in qualities)
    if delta + error > LIMIT:
        return {"quality": "unavailable", "reason": "duration_overflow"}
    return {
        "quality": "exact" if error == 0 else "uncertain" if error >= delta else "bounded",
        "nominal_ns": delta,
        "lower_ns": max(0, delta - error),
        "upper_ns": delta + error,
        "reason": "declared_clock_bounds",
    }


def monotonic_duration(start: dict, end: dict) -> tuple[int | None, str | None]:
    """Opt-in analysis extension; no inference from adjacent seq or recorded_at."""
    values = [e.get("extensions", {}).get("sledra.timing") for e in (start, end)]
    if not any(v is not None for v in values):
        return None, None
    for value in values:
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "clock_id", "monotonic_ns"}
            or value["version"] != "1"
            or not isinstance(value["clock_id"], str)
            or not value["clock_id"]
            or type(value["monotonic_ns"]) is not int
            or not 0 <= value["monotonic_ns"] <= LIMIT
        ):
            return None, "invalid_monotonic_evidence"
    domain = ("source_id", "session_id", "boot_id")
    if (
        values[0]["clock_id"] != values[1]["clock_id"]
        or not start["source"].get("boot_id")
        or any(start["source"].get(k) != end["source"].get(k) for k in domain)
    ):
        return None, "monotonic_domain_mismatch"
    delta = values[1]["monotonic_ns"] - values[0]["monotonic_ns"]
    return (None, "negative_monotonic_duration") if delta < 0 else (delta, None)
