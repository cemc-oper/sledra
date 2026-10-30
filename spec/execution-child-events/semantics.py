"""Pure decision oracles for shared vectors; never call these from the recorder."""

from __future__ import annotations

import re

from ce_contract import ContractError, argv_payload, authorize, fail, legacy


def admission(case: dict) -> dict:
    """Maximum permitted append, not a simulation or proof of actual I/O."""
    phase, kind = case.get("phase", "active"), case.get("kind", "event")

    def answer(result, rc, append_max=0, next_phase=phase):
        return {"result": result, "rc": rc, "append_max": append_max, "phase": next_phase}

    if case.get("off") or not case.get("attached", True):
        return answer("silent", None)
    if not case.get("identity_valid", True):
        return answer("unavailable", 26)
    if not case.get("input_valid", True):
        return answer("rejected", 20)
    try:
        authorize(case.get("role", "event"), kind)
    except ContractError:
        return answer("rejected", 20)
    if (
        not case.get("control_valid", True)
        or phase in ("disabled", "creating")
        or case.get("disabled")
    ):
        return answer("disabled", 23, next_phase="disabled")
    ending = kind == "end"
    closing = case.get("closing", False)
    if phase == "closed":
        return answer(
            "noop" if ending and case.get("same_end") else "closed",
            0 if ending and case.get("same_end") else 22,
        )
    if closing and (not ending or not case.get("same_end")):
        return answer("closed", 22, next_phase="closing")
    if not case.get("lock_free", True):
        return answer("busy", 21, next_phase="closing" if ending else phase)
    if phase == "writing":
        return answer("disabled", 23, next_phase="disabled")
    if kind == "context" and case.get("context") == "same":
        return answer("noop", 0)
    if kind == "context" and case.get("context") == "conflict":
        return answer("disabled", 23, next_phase="disabled")
    if kind in ("stage-start", "stage-end") and not case.get("stage_valid", True):
        return answer("rejected", 20)
    if ending and case.get("end_attempted"):
        return answer("disabled", 23, next_phase="disabled")
    return answer("admit", 0, 1, "writing")


def recovery(case: dict) -> dict:
    confirmed = (
        case.get("lock_free", True)
        and case.get("control_valid", True)
        and case.get("phase") != "writing"
        and case.get("receipt_valid", True)
        and case.get("last") is not None
        and all(
            case["last"].get(k) == case["request"].get(k)
            for k in ("request_id", "event_id", "record_sha256")
        )
    )
    return {
        "result": "confirmed" if confirmed else "unknown",
        "disable_attempts": 0 if confirmed else 1,
        "prepare_again": False,
        "append_again": False,
        "phase": case["phase"],
    }


def diagnostic(case: dict) -> dict:
    if case["reason"] in ("off", "missing_reference", "unattached", "busy", "closed", "noop"):
        return {"emit": False, "budget": "none"}
    shared = case["shared_available"]
    return {
        "emit": not case["shared_used" if shared else "local_used"],
        "budget": "execution" if shared else "process",
    }


def compatibility(case: dict) -> str:
    if case["mode"] not in ("off", "execution-v2") or case.get("ambiguous", False):
        return "incompatible"
    if case["mode"] == "off":
        return "silent"
    if not case["child_events"]:
        return "legacy" if case["binary_legacy"] else "incompatible"
    if not case["library_ce"]:
        return "rejected"
    if type(case["binary_protocol"]) is not int or (
        case["binary_interface"],
        case["binary_protocol"],
    ) != ("CE-1", 1):
        return "incompatible"
    return "coordinated"


def typed_values(args: list[str]) -> dict:
    argv_payload(args)
    values = {}
    while args:
        option, *args = args
        arity = 1 if option == "--null" else 2
        if option not in ("--null", "--string", "--int", "--float", "--bool") or len(args) < arity:
            fail("usage")
        key, *rest = args[:arity]
        args = args[arity:]
        if key in values:
            fail("duplicate_key")
        if option == "--null":
            value = None
        elif option == "--string":
            value = rest[0]
        else:
            token = rest[0]
            try:
                value = legacy.decode(token.encode("utf-8"))
            except ContractError:
                fail("invalid_value")
            if option == "--bool":
                if token not in ("true", "false"):
                    fail("invalid_value")
            else:
                number = re.fullmatch(
                    r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?", token
                )
                if not number or (any(c in token for c in ".eE") != (option == "--float")):
                    fail("invalid_value")
        values[key] = value
    legacy.boundaries(values)
    return values
