"""Pair explicit identities, retain incomplete samples, and never sum parallel work."""

from __future__ import annotations

from collections import Counter, defaultdict
from fractions import Fraction

from sledra_analysis.metrics.clock import monotonic_duration, utc_ns, wall_interval
from sledra_analysis.process_pairing import process_pairing
from sledra_analysis.projections.reducer import event_ref
from sledra_analysis.store.canonical import canonical_bytes, canonical_hash

METRICS_VERSION = "sledra-timing-1.2.0"


def _unique(events):
    # Identical re-reports with different IDs may share a boundary only when their
    # full semantics and clock/source identity agree. All source refs remain below.
    return list(
        {
            canonical_bytes(
                {
                    k: v
                    for k, v in e.items()
                    if k not in ("event_id", "recorded_at", "evidence_refs")
                }
            ): e
            for e in events
        }.values()
    )


def _outcome(end):
    p = end["payload"]
    if "termination" in p:
        return "failure" if p["termination"] == "signaled" or p.get("exit_code", 1) else "success"
    value = p.get("result", p.get("outcome"))
    return (
        "failure"
        if value in ("failure", "timeout", "error", "not_accepted")
        else "success"
        if value in ("success", "ready") or end["event_type"] == "submission.accepted"
        else "unknown"
    )


def pair(metric, key, starts, ends, *, report_as_of, refs, flags=()):
    starts, ends = _unique(starts), _unique(ends)
    row = {
        "metric": metric,
        "key": list(key),
        "because": sorted(set(refs)),
        "pairing": "paired"
        if starts and ends
        else "start_only"
        if starts
        else "end_only"
        if ends
        else "unavailable",
        "duration_ns": None,
        "duration_basis": None,
        "clock": None,
        "outcome": _outcome(ends[0]) if len(ends) == 1 else "unknown",
        "right_censored": False,
        "observed_lower_bound_ns": None,
        "flags": sorted(set(flags)),
        "start_at": starts[0].get("occurred_at") if len(starts) == 1 else None,
        "end_at": ends[0].get("occurred_at") if len(ends) == 1 else None,
    }
    if metric == "process_execution":
        pairing, reason = process_pairing(starts, ends)
        if pairing == "conflict":
            row["pairing"] = pairing
            row["flags"].append(reason)
            return row
    if (
        len(starts) > 1
        or len(ends) > 1
        or set(flags) & {"conflict", "parent_mismatch", "association_uncertain", "orphan"}
    ):
        row["pairing"] = "conflict" if starts or ends else "unavailable"
        return row
    for event in starts + ends:
        try:
            if utc_ns(event.get("occurred_at")) > utc_ns(report_as_of):
                row["flags"].append("after_report_as_of")
        except (ValueError, TypeError, OverflowError):
            pass
    if starts and not ends:
        try:
            row["right_censored"] = utc_ns(starts[0].get("occurred_at")) <= utc_ns(report_as_of)
        except (ValueError, TypeError, OverflowError):
            row["right_censored"] = False
        row["clock"] = wall_interval(
            starts[0],
            {
                "occurred_at": report_as_of,
                "time_quality": {
                    "basis": "producer_clock",
                    "precision_ns": 0,
                    "clock_error_bound_ns": 0,
                },
            },
        )
        row["observed_lower_bound_ns"] = row["clock"].get("lower_ns")
    if starts and ends:
        row["clock"] = wall_interval(starts[0], ends[0])
        duration, error = monotonic_duration(starts[0], ends[0])
        declared = ends[0]["payload"].get("duration_ns") if metric == "stage" else None
        if error:
            row["flags"].append(error)
        elif duration is not None:
            if declared is not None and declared != duration:
                row["flags"].append("duration_evidence_conflict")
            else:
                row["duration_ns"], row["duration_basis"] = duration, "monotonic"
        elif declared is not None:
            row["duration_ns"], row["duration_basis"] = declared, "producer_duration"
        elif row["clock"]["quality"] == "exact":
            row["duration_ns"], row["duration_basis"] = (
                row["clock"]["nominal_ns"],
                "utc_declared_bounds",
            )
        if (
            row["duration_ns"] is not None
            and "lower_ns" in row["clock"]
            and not row["clock"]["lower_ns"] <= row["duration_ns"] <= row["clock"]["upper_ns"]
        ):
            row["flags"].append("wall_duration_disagrees")
        if row["clock"]["quality"] == "conflict":
            row["flags"].append("wall_clock_conflict")
        if error or "duration_evidence_conflict" in row["flags"]:
            row["pairing"] = "conflict"
        if "after_report_as_of" in row["flags"]:
            row["duration_ns"] = None
    # A measured end duration never manufactures a missing start endpoint.
    if ends and not starts and "duration_ns" in ends[0]["payload"]:
        row["unpaired_declared_duration_ns"] = ends[0]["payload"]["duration_ns"]
    row["flags"] = sorted(set(row["flags"]))
    return row


def _rational(value):
    value = Fraction(value)
    # Large accumulations are encoded as decimal strings, not unsafe JSON numbers.
    return {"numerator": str(value.numerator), "denominator": str(value.denominator)}


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[canonical_bytes(row["stratum"])].append(row)
    result = []
    for samples in groups.values():
        values = sorted(
            r["duration_ns"]
            for r in samples
            if r["pairing"] == "paired" and r["duration_ns"] is not None
        )
        n = len(values)
        counts = Counter(r["pairing"] for r in samples)
        outcomes = Counter(r["outcome"] for r in samples)
        result.append(
            {
                "stratum": samples[0]["stratum"],
                "total": len(samples),
                "pairing_counts": dict(sorted(counts.items())),
                "outcome_counts": dict(sorted(outcomes.items())),
                "missing_start": sum(r["pairing"] == "end_only" for r in samples),
                "missing_end": sum(r["pairing"] == "start_only" for r in samples),
                "unavailable": counts["unavailable"],
                "right_censored": sum(r["right_censored"] for r in samples),
                "flag_counts": dict(
                    sorted(Counter(flag for r in samples for flag in set(r["flags"])).items())
                ),
                "clock_quality_counts": dict(
                    sorted(
                        Counter(
                            (r["clock"] or {}).get("quality", "unavailable") for r in samples
                        ).items()
                    )
                ),
                "incomplete": sum(
                    r["pairing"] != "paired" or r["duration_ns"] is None for r in samples
                ),
                "statistics": {
                    "condition": "paired, exact_or_explicit_duration, endpoints_not_after_report_as_of; all outcomes",
                    "n": n,
                    "failure_n": sum(
                        r["outcome"] == "failure"
                        and r["duration_ns"] is not None
                        and r["pairing"] == "paired"
                        for r in samples
                    ),
                    "mean_ns": _rational(Fraction(sum(values), n)) if n else None,
                    "median_ns": _rational(Fraction(values[(n - 1) // 2] + values[n // 2], 2))
                    if n
                    else None,
                    "p95_ns": values[(95 * n + 99) // 100 - 1] if n else None,
                    "quantile_method": "nearest_rank",
                    "duration_basis_counts": dict(
                        sorted(
                            Counter(
                                r["duration_basis"]
                                for r in samples
                                if r["duration_ns"] is not None and r["pairing"] == "paired"
                            ).items()
                        )
                    ),
                },
            }
        )
    return sorted(result, key=lambda item: canonical_bytes(item["stratum"]))


def reconstruct_timing(
    facts, documents, state, *, report_as_of, accounting=(), cycle_window=None, engine_mapping=None
):
    runs = {r["id"]: r for r in state["runs"]}
    attempts = {a["id"]: a for a in state["attempts"]}
    jobs = {j["id"]: j for j in state["jobs"]}
    rejected = {r["fact"] for r in state["rejected_facts"]}
    diagnostics, groups, rows = [], defaultdict(lambda: [[], [], []]), []
    selected = set(runs)
    unlocated_cycles = []
    if cycle_window:
        lo, hi = map(utc_ns, cycle_window)
        selected = set()
        for rid, run in runs.items():
            try:
                stamp = utc_ns(run["context"]["cycle"]["cycle_time"])
            except (ValueError, TypeError, KeyError, OverflowError):
                unlocated_cycles.append(rid)
                diagnostics.append(
                    {"run_id": rid, "reason": "cycle_time_unavailable", "because": run["documents"]}
                )
                continue
            if lo <= stamp < hi:
                selected.add(rid)

    def add(metric, key, side, f):
        group = groups[(metric, tuple(key))]
        group[side].append(f["event"])
        group[2].append(event_ref(f))

    for f in facts:
        e, ref = f["event"], event_ref(f)
        if e["association"]["run_id"] not in selected:
            continue
        if f["status"] != "known" or ref in rejected:
            diagnostics.append({"fact": ref, "reason": f.get("reason") or "rejected_association"})
            continue
        a, p, name = e["association"], e["payload"], e["event_type"]
        base = [a["run_id"], a.get("attempt_id")]
        if name in ("attempt.started", "attempt.exited"):
            add("process_execution", base + [p["process_role"]], name.endswith("exited"), f)
        elif name in ("stage.started", "stage.ended"):
            add("stage", base + [p["stage_key"], p["interval_id"]], name.endswith("ended"), f)
        elif name in ("data.wait_started", "data.wait_ended"):
            add("dependency_wait", base + [p["wait_id"], p["selector"]], name.endswith("ended"), f)
        elif name.startswith("submission."):
            side = name != "submission.requested"
            metric = (
                "submission_unknown"
                if name == "submission.failed" and p["outcome"] == "unknown"
                else "submission_processing"
            )
            add(metric, base + [p["submission_instance"]], side, f)
        elif name == "engine.state_observed":
            if (
                engine_mapping
                and p["raw_state"] in engine_mapping["submitted"] + engine_mapping["active"]
            ):
                side = p["raw_state"] in engine_mapping["active"]
                normalized = dict(e, occurred_at=p["observation_time"])
                group = groups[
                    (
                        "engine_observed_interval",
                        (
                            a["run_id"],
                            None,
                            p["engine_instance"],
                            p["node_path"],
                            e["source"]["source_id"],
                            e["source"]["session_id"],
                        ),
                    )
                ]
                group[side].append(normalized)
                group[2].append(ref)
            else:
                diagnostics.append(
                    {
                        "fact": ref,
                        "reason": "engine_transition_mapping_unavailable",
                        "observation": p,
                    }
                )
        elif name == "artifact.validation_completed":
            diagnostics.append(
                {
                    "fact": ref,
                    "reason": "check_interval_unavailable",
                    "completed_at": e["occurred_at"],
                }
            )
        elif name == "job.state_observed":
            observed = dict(e, occurred_at=p["observation_time"])
            group = groups[
                ("scheduler_sampled_transition", tuple([a["run_id"], None, p["job_ref_id"]]))
            ]
            group[2].append(ref)
            if p["normalized_state"] in ("queued", "running"):
                group[p["normalized_state"] == "running"].append(observed)
    # Unknown submission is a censored processing sample plus an observed lower
    # bound to the ambiguous response, never a completed queue/rejection sample.
    for (metric, key), (_, ends, refs) in list(groups.items()):
        if metric != "submission_unknown":
            continue
        processing = groups[("submission_processing", key)]
        processing[2].extend(refs)
        if processing[0] and ends:
            diagnostics.append(
                {
                    "because": sorted(refs),
                    "reason": "submission_outcome_unknown",
                    "clock": wall_interval(processing[0][0], ends[0])
                    if len(processing[0]) == len(ends) == 1
                    else None,
                }
            )
        del groups[(metric, key)]
    process_attempts = {key[1] for metric, key in groups if metric == "process_execution"}
    stage_attempts = {key[1:3] for metric, key in groups if metric == "stage"}
    declared_stages = defaultdict(set)
    for doc in documents:
        if doc["kind"] == "definition_manifest":
            for task in doc["tasks"]:
                if task.get("stage_key"):
                    declared_stages[(doc["definition_id"], task["task_key"])].add(task["stage_key"])
    for aid, attempt in attempts.items():
        if len(attempt["run_ids"]) != 1 or attempt["run_ids"][0] not in selected:
            continue
        c = attempt.get("context")
        if c is None:
            continue
        if aid not in process_attempts:
            groups[("process_execution", (c["run_id"], aid, c["process_role"]))][2].extend(
                attempt["documents"]
            )
        rc = runs[c["run_id"]].get("context") or {}
        definition_id = rc.get("definition_ref", {}).get("id")
        for stage in sorted(declared_stages[(definition_id, c["task_key"])]):
            if (aid, stage) not in stage_attempts:
                groups[("stage", (c["run_id"], aid, stage, None))][2].extend(attempt["documents"])

    def attach(row, rid, aid=None):
        rc = (runs.get(rid) or {}).get("context") or {}
        ac = (attempts.get(aid) or {}).get("context") or {}
        row["run_id"], row["attempt_id"] = rid, aid
        row["stratum"] = {
            "metric": row["metric"],
            "detail": row["key"][2:3]
            if row["metric"] == "stage"
            else row["key"][3:4]
            if row["metric"] == "dependency_wait"
            else [],
            "purpose": rc.get("purpose"),
            "definition_ref": rc.get("definition_ref"),
            "config_refs": rc.get("config_refs"),
            "platform_id": rc.get("platform_id"),
            "task_key": ac.get("task_key"),
            "parameters": ac.get("parameters"),
            "rules": METRICS_VERSION,
        }
        if row["metric"] == "engine_observed_interval":
            row["stratum"]["engine_mapping_version"] = engine_mapping["version"]
        row["id"] = "int_" + canonical_hash([row["metric"], row["key"]])
        rows.append(row)

    for (metric, key), (starts, ends, refs) in sorted(
        groups.items(), key=lambda item: canonical_bytes([item[0][0], list(item[0][1])])
    ):
        node = (
            attempts.get(key[1])
            if key[1]
            else jobs.get(key[2])
            if metric == "scheduler_sampled_transition"
            else runs.get(key[0])
        )
        flags = (node or runs.get(key[0]) or {}).get("observability", {}).get("flags", [])
        if metric == "scheduler_sampled_transition":
            # A polling transition brackets a transition time, not total queue time.
            try:
                latest_queued = (
                    max(starts, key=lambda e: utc_ns(e["occurred_at"])) if starts else None
                )
                first_running = min(ends, key=lambda e: utc_ns(e["occurred_at"])) if ends else None
                row = pair(
                    metric,
                    key,
                    [latest_queued] if latest_queued else [],
                    [first_running] if first_running else [],
                    report_as_of=report_as_of,
                    refs=refs,
                    flags=flags,
                )
                row["duration_ns"], row["duration_basis"] = None, None
                row["transition_window"] = {
                    "after": row["start_at"],
                    "no_later_than": row["end_at"],
                }
                if row["clock"] and row["clock"]["quality"] == "conflict":
                    row["pairing"] = "conflict"
            except (ValueError, TypeError, OverflowError):
                row = pair(
                    metric,
                    key,
                    [],
                    [],
                    report_as_of=report_as_of,
                    refs=refs,
                    flags=[*flags, "unknown_time_or_timezone"],
                )
        else:
            row = pair(metric, key, starts, ends, report_as_of=report_as_of, refs=refs, flags=flags)
        attach(row, key[0], key[1])
    for record in accounting:
        rid, jid = record["run_id"], record["job_ref_id"]
        if rid not in selected:
            continue
        job = jobs.get(jid)
        if not job or job["run_ids"] != [rid] or not job.get("context"):
            raise ValueError("accounting requires a resolved composite Job reference")
        jc = job["context"]
        if jc["submission_instance"] != record["submission_instance"]:
            raise ValueError("accounting submission generation mismatch")
        row = pair(
            "scheduler_accounting_queue",
            [rid, jc["attempt_id"], jid, record["interval_id"]],
            [dict(record["start"], payload={}, source={})],
            [dict(record["end"], payload={}, source={}, event_type="scheduler.accounting")],
            report_as_of=report_as_of,
            refs=record["because"],
            flags=job["observability"]["flags"],
        )
        row["queue_basis"], row["includes_hold"] = record["queue_basis"], record["includes_hold"]
        attach(row, rid, jc["attempt_id"])
        row["stratum"].update(
            queue_basis=record["queue_basis"],
            includes_hold=record["includes_hold"],
            adapter_version=record["adapter_version"],
        )
    return {
        "intervals": sorted(rows, key=lambda row: row["id"]),
        "statistics": summarize(rows),
        "diagnostics": sorted(diagnostics, key=canonical_bytes),
        "source_health": state["source_health"],
        "observed_runs": len(selected),
        "unlocated_cycle_run_ids": sorted(unlocated_cycles),
        "cycle_window": cycle_window,
        "report_as_of": report_as_of,
        "critical_path": {
            "state": "unavailable",
            "reason": "dynamic_dependency_evidence_unavailable",
        },
        "run_elapsed_ns": None,
        "run_elapsed_reason": "parallel_intervals_are_not_additive",
        "product_delivery_timeliness": "unavailable_until_versioned_product_rules",
    }
