"""Shared identity and endpoint decision for attempt process facts."""

from sledra_analysis.store.canonical import canonical_bytes


def _unique(events):
    return {
        canonical_bytes(
            {
                k: v
                for k, v in event.items()
                if k not in ("event_id", "recorded_at", "evidence_refs")
            }
        )
        for event in events
    }


def process_pairing(starts, ends):
    """Return (pairing, reason); no clock or business outcome is inferred."""
    if len(_unique(starts)) > 1:
        return "conflict", "multiple_process_starts"
    if len(_unique(ends)) > 1:
        return "conflict", "multiple_process_ends"
    if not starts and not ends:
        return "unavailable", "missing_process_endpoints"
    if not starts:
        return "end_only", "missing_process_start"
    if not ends:
        return "start_only", "missing_process_end"
    start, end = starts[0], ends[0]
    for container, keys in (
        ("association", ("run_id", "task_instance_id", "attempt_id")),
        ("payload", ("process_role",)),
        ("source", ("source_id", "session_id")),
    ):
        left, right = start.get(container) or {}, end.get(container) or {}
        if any(not left.get(key) or not right.get(key) for key in keys):
            return "conflict", "process_identity_unavailable"
        if any(left[key] != right[key] for key in keys):
            return "conflict", "process_invocation_association_uncertain"
    if (start.get("source") or {}).get("boot_id") != (end.get("source") or {}).get("boot_id"):
        return "conflict", "process_invocation_association_uncertain"
    return "paired", "process_identity_matched"
