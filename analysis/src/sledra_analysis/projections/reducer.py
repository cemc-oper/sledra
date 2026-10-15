"""Causal reconstruction: no arrival order, clock winner, or inferred business success."""

from __future__ import annotations

from collections import defaultdict

from sledra_analysis.process_pairing import process_pairing
from sledra_analysis.projections.documents import KINDS, check_parameters, instance_id
from sledra_analysis.store.canonical import RecordError, canonical_bytes, canonical_hash

REDUCER_VERSION = "sledra-state-reducer-1.2.0"
RULE_VERSION = "sledra-state-rules-1.2.0"


def event_ref(fact):
    return fact["event"]["event_id"] + "@" + fact["hash"]


def doc_ref(doc):
    return "document:" + doc["kind"] + ":" + doc[KINDS[doc["kind"]]] + "@" + canonical_hash(doc)


def dimension(state, refs=(), **details):
    return {"state": state, "because": sorted(set(refs)), **details}


def _node(key):
    return {
        "id": key,
        "flags": set(),
        "evidence": set(),
        "documents": set(),
        "_facts": [],
        "_runs": set(),
        "_instances": set(),
        "_attempts": set(),
    }


def _select(facts, kind):
    return [f for f in facts if f["event"]["event_type"] == kind]


def _refs(facts):
    return [event_ref(f) for f in facts]


def _values(facts, transform):
    return {canonical_bytes(transform(f["event"])) for f in facts}


def _reaches(key, parents):
    seen = set()
    while key is not None and key in parents:
        if key in seen:
            return True
        seen.add(key)
        key = parents[key]
    return False


def _execution(node, context):
    facts = node["_facts"]
    starts, ends = _select(facts, "attempt.started"), _select(facts, "attempt.exited")
    role = context.get("process_role") if context else None
    roles = {f["event"]["payload"]["process_role"] for f in starts + ends}
    if role is None and len(roles) == 1:
        role = next(iter(roles))
    if roles - {role}:
        node["flags"].add("association_uncertain")
    starts = [f for f in starts if f["event"]["payload"]["process_role"] == role]
    ends = [f for f in ends if f["event"]["payload"]["process_role"] == role]
    refs = _refs(starts + ends)
    if any(
        flag in node["flags"] for flag in ("conflict", "parent_mismatch", "association_uncertain")
    ):
        return dimension("conflict", node["evidence"], process_role=role)
    pairing, reason = process_pairing([f["event"] for f in starts], [f["event"] for f in ends])
    if pairing == "conflict":
        node["flags"].add("conflict")
        return dimension("conflict", refs, process_role=role, pairing_reason=reason)
    if ends:
        end = ends[0]["event"]["payload"]
        if not starts:
            node["flags"].add("missing_start")
        return dimension(
            "exited"
            if starts and end["termination"] == "exited"
            else "signaled"
            if starts
            else "end_only",
            refs,
            process_role=role,
            termination=end["termination"],
            **{k: end[k] for k in ("exit_code", "signal") if k in end},
        )
    if starts:
        node["flags"].add("missing_end")
        return dimension("started", refs, process_role=role)
    return dimension(
        "registered_only" if _select(facts, "attempt.registered") else "unknown",
        _refs(_select(facts, "attempt.registered")),
        process_role=role,
    )


def _stages(node):
    groups = defaultdict(list)
    for f in node["_facts"]:
        if f["event"]["event_type"] in ("stage.started", "stage.ended"):
            p = f["event"]["payload"]
            groups[p["stage_key"], p["interval_id"]].append(f)
    result = []
    for (stage, interval), facts in sorted(groups.items()):
        starts, ends = _select(facts, "stage.started"), _select(facts, "stage.ended")
        conflicting = (
            len(_values(starts, lambda e: e["occurred_at"])) > 1
            or len(_values(ends, lambda e: [e["payload"], e["occurred_at"]])) > 1
        )
        state = (
            "conflict"
            if conflicting
            else ends[0]["event"]["payload"]["result"]
            if starts and ends
            else "end_only"
            if ends
            else "start_only"
        )
        if state in ("start_only", "end_only"):
            node["flags"].add("stage_incomplete")
        if state == "conflict":
            node["flags"].add("stage_conflict")
        result.append(
            {"stage_key": stage, "interval_id": interval, **dimension(state, _refs(facts))}
        )
    return result


def _selection(instance, ids, leaves, facts, rejected):
    declarations = _select(facts, "attempt.selection_declared")
    if not declarations:
        return (leaves[0] if len(leaves) == 1 else None), dimension(
            "retry_chain" if len(leaves) == 1 else "ambiguous",
            instance["evidence"],
            candidate_attempt_ids=leaves,
        )
    by_id = defaultdict(list)
    for f in declarations:
        by_id[f["event"]["payload"]["selection_id"]].append(f)
    parents, valid = {}, {}
    for key, versions in by_id.items():
        if len(_values(versions, lambda e: e["payload"])) != 1:
            return None, dimension("conflict", _refs(declarations))
        p = versions[0]["event"]["payload"]
        parents[key] = p.get("supersedes_selection_id")
        valid[key] = p
    if any(parent is not None and parent not in parents for parent in parents.values()) or any(
        _reaches(key, parents) for key in parents
    ):
        return None, dimension("selection_chain_unresolved", _refs(declarations))
    heads = sorted(set(parents) - set(parents.values()))
    if len(heads) != 1:
        return None, dimension("ambiguous", _refs(declarations))
    head = valid[heads[0]]
    candidates = set(head["candidate_attempt_ids"])
    if not candidates <= set(ids) or not set(leaves) <= candidates:
        rejected.extend(
            {
                "fact": event_ref(f),
                "reason": "selection_scope_unresolved",
                "object_id": instance["id"],
            }
            for f in by_id[heads[0]]
        )
        return None, dimension(
            "selection_stale_or_orphan", _refs(declarations), candidate_attempt_ids=leaves
        )
    chosen = valid[heads[0]]["selected_attempt_id"]
    return chosen, dimension(
        "explicit_selection",
        _refs(declarations),
        selection_id=heads[0],
        candidate_attempt_ids=valid[heads[0]]["candidate_attempt_ids"],
    )


def reconstruct(facts: list[dict], documents: list[dict], health: list[dict]) -> dict:
    """Return a canonicalizable state. Inputs include every version and its disposition."""
    runs, instances, attempts, jobs = ({}, {}, {}, {})
    maps = {"run": runs, "instance": instances, "attempt": attempts, "job": jobs}
    docgroups = defaultdict(dict)
    rejected = []
    for doc in documents:
        docgroups[doc["kind"], doc[KINDS[doc["kind"]]]][canonical_hash(doc)] = doc

    def unique(kind, key):
        values = docgroups.get((kind, key), {})
        return next(iter(values.values())) if len(values) == 1 else None

    def node(kind, key):
        return maps[kind].setdefault(key, _node(key))

    def association(event):
        a = event.get("association", {})
        return {k: v for k, v in a.items() if isinstance(v, str) and v}

    # Every claim remains visible, including contradictory or opaque associations.
    for f in facts:
        a = association(f["event"])
        for field, kind in [
            ("run_id", "run"),
            ("task_instance_id", "instance"),
            ("attempt_id", "attempt"),
            ("job_ref_id", "job"),
        ]:
            if field not in a:
                continue
            n = node(kind, a[field])
            n["evidence"].add(event_ref(f))
            n["_runs"].update([a["run_id"]] if "run_id" in a else [])
            n["_instances"].update([a["task_instance_id"]] if "task_instance_id" in a else [])
            if kind == "job" and "attempt_id" in a:
                n["_attempts"].add(a["attempt_id"])
            if f["status"] != "known":
                n["flags"].add(
                    "conflict"
                    if f["status"] == "conflict"
                    else "unsupported_semantics"
                    if f["status"] == "opaque"
                    else "invalid_fact"
                )
    for (kind, key), versions in sorted(docgroups.items()):
        target = {"run_context": "run", "attempt_context": "attempt", "job_ref": "job"}.get(kind)
        if target is None:
            continue
        n = node(target, key)
        n["documents"].update(doc_ref(d) for d in versions.values())
        if len(versions) > 1:
            n["flags"].add("conflict")
        for d in versions.values():
            n["_runs"].add(d["run_id"])
            node("run", d["run_id"])["documents"].add(doc_ref(d))
            if kind == "attempt_context":
                i = node("instance", d["task_instance_id"])
                i["_runs"].add(d["run_id"])
                i["documents"].add(doc_ref(d))
                n["_instances"].add(d["task_instance_id"])
            if kind == "job_ref":
                n["_attempts"].add(d["attempt_id"])
    # Resolve immutable Run -> Definition -> expanded scope before event semantics.
    for rid, r in list(runs.items()):
        context = unique("run_context", rid)
        r["context"] = context
        r["scope_instance_ids"] = []
        if not context:
            r["flags"].update(("orphan", "source_unknown"))
            continue
        definition = unique("definition_manifest", context["definition_ref"]["id"])
        if definition is None:
            r["flags"].add("orphan")
            continue
        r["documents"].add(doc_ref(definition))
        if context["definition_ref"]["sha256"] != definition["content_sha256"]:
            r["flags"].add("parent_mismatch")
            continue
        tasks = {t["task_key"]: t for t in definition["tasks"]}
        seen = set()
        for entry in context["scope"]:
            iid = instance_id(rid, entry["task_key"], entry["parameters"])
            i = node("instance", iid)
            i["_runs"].add(rid)
            i["task_key"], i["parameters"] = entry["task_key"], entry["parameters"]
            i["documents"].update((doc_ref(context), doc_ref(definition)))
            try:
                if entry["task_key"] not in tasks or iid in seen:
                    raise RecordError("invalid run scope", "parent_mismatch")
                check_parameters(entry["parameters"], tasks[entry["task_key"]])
            except RecordError:
                i["flags"].add("parent_mismatch")
                r["flags"].add("parent_mismatch")
            seen.add(iid)
        r["scope_instance_ids"] = sorted(seen)
    for aid, a in attempts.items():
        context = unique("attempt_context", aid)
        a["context"] = context
        if context:
            r = runs[context["run_id"]]
            parent = r.get("context")
            if not parent:
                a["flags"].add("orphan")
            elif context["run_context_sha256"] != parent["context_sha256"]:
                a["flags"].add("parent_mismatch")
            elif "orphan" in r["flags"]:
                a["flags"].add("orphan")
            elif (
                context["task_instance_id"] not in r["scope_instance_ids"]
                or "parent_mismatch" in r["flags"]
                or "parent_mismatch" in instances[context["task_instance_id"]]["flags"]
            ):
                a["flags"].add("parent_mismatch")
        else:
            a["flags"].update(("orphan", "source_unknown"))
    for kind, entities in maps.items():
        for n in entities.values():
            if (
                len(n["_runs"]) != 1
                or (kind == "attempt" and len(n["_instances"]) != 1)
                or (kind == "job" and len(n["_attempts"]) > 1)
            ):
                n["flags"].add("parent_mismatch")
    # Reject a cross-parent fact from semantic use, retain its raw identity and all claims.
    for f in facts:
        if f["status"] != "known":
            continue
        e = f["event"]
        a = association(e)
        p = e["payload"]
        name = e["event_type"]
        bad = False
        ac = unique("attempt_context", a.get("attempt_id"))
        rc = unique("run_context", a.get("run_id"))
        jc = unique("job_ref", a.get("job_ref_id"))
        if ac and (
            ac["run_id"] != a.get("run_id") or ac["task_instance_id"] != a.get("task_instance_id")
        ):
            bad = True
        if jc and (
            jc["run_id"] != a.get("run_id")
            or ("attempt_id" in a and jc["attempt_id"] != a["attempt_id"])
        ):
            bad = True
        if (
            name == "run.registered"
            and rc
            and any(
                p[k] != rc[v]
                for k, v in [
                    ("run_context_sha256", "context_sha256"),
                    ("request_sha256", "request_sha256"),
                ]
            )
        ):
            bad = True
        if (
            name == "attempt.registered"
            and ac
            and any(
                p[k] != ac[v]
                for k, v in [
                    ("attempt_context_sha256", "context_sha256"),
                    ("execution_token_sha256", "execution_token_sha256"),
                    ("retry_of", "retry_of"),
                ]
            )
        ):
            bad = True
        if name in ("submission.accepted", "job.state_observed") and (
            p["job_ref_id"] != a.get("job_ref_id")
            or (
                name == "submission.accepted"
                and jc
                and p["submission_instance"] != jc["submission_instance"]
            )
        ):
            bad = True
        for field, kind in [
            ("run_id", "run"),
            ("task_instance_id", "instance"),
            ("attempt_id", "attempt"),
            ("job_ref_id", "job"),
        ]:
            if field in a:
                n = node(kind, a[field])
                if bad:
                    n["flags"].add("parent_mismatch")
                else:
                    n["_facts"].append(f)
        if bad:
            rejected.append(
                {
                    "fact": event_ref(f),
                    "reason": "parent_mismatch",
                    "object_id": a.get("attempt_id", a.get("run_id")),
                }
            )
    # Job documents must also resolve the owning Attempt and platform; scheduler IDs alone never join.
    for jid, j in jobs.items():
        jc = unique("job_ref", jid)
        if not jc:
            j["flags"].update(("orphan", "source_unknown"))
        else:
            ac = unique("attempt_context", jc["attempt_id"])
            rc = unique("run_context", jc["run_id"])
            if ac is None or rc is None:
                j["flags"].add("orphan")
            elif ac["run_id"] != jc["run_id"] or rc["platform_id"] != jc["platform_id"]:
                j["flags"].add("parent_mismatch")
        observations = _select(j["_facts"], "job.state_observed")
        states = sorted({f["event"]["payload"]["normalized_state"] for f in observations})
        j["observations"] = [
            {"fact": event_ref(f), **f["event"]["payload"]}
            for f in sorted(observations, key=event_ref)
        ]
        j["scheduler"] = dimension(
            "conflict"
            if "parent_mismatch" in j["flags"] or "conflict" in j["flags"]
            else "observed_" + states[0]
            if len(states) == 1
            else "multiple_observations"
            if states
            else "unknown",
            _refs(observations),
            observed_states=states,
        )
        j["context"] = jc
    for aid, a in attempts.items():
        ac = a["context"]
        registered = _select(a["_facts"], "attempt.registered")
        if not registered:
            a["flags"].add("source_unknown")
        a["registration"] = dimension(
            "registered" if registered else "source_unknown", _refs(registered)
        )
        retry_claims = {f["event"]["payload"]["retry_of"] for f in registered}
        if ac:
            retry_claims.add(ac["retry_of"])
        if len(retry_claims) > 1:
            a["flags"].add("conflict")
        a["retry_of"] = next(iter(retry_claims)) if len(retry_claims) == 1 else None
        a["execution"] = _execution(a, ac)
        a["stages"] = _stages(a)
        a["job_ref_ids"] = sorted(jid for jid, j in jobs.items() if aid in j["_attempts"])
        primary = [
            jid
            for jid in a["job_ref_ids"]
            if jobs[jid]["context"] and jobs[jid]["context"]["role"] == "primary"
        ]
        if len(primary) > 1:
            a["flags"].add("association_uncertain")
        a["submission_observations"] = [
            {"fact": event_ref(f), "event_type": f["event"]["event_type"], **f["event"]["payload"]}
            for f in sorted(a["_facts"], key=event_ref)
            if f["event"]["event_type"].startswith("submission.")
        ]
    for iid, i in instances.items():
        ids = sorted(aid for aid, a in attempts.items() if iid in a["_instances"])
        i["attempt_ids"] = ids
        parents = {aid: attempts[aid]["retry_of"] for aid in ids}
        invalid_graph = False
        for aid, parent in parents.items():
            if parent is not None and parent not in ids:
                attempts[aid]["flags"].add(
                    "orphan" if parent not in attempts else "parent_mismatch"
                )
                invalid_graph = True
            if _reaches(aid, parents):
                attempts[aid]["flags"].add("retry_cycle")
                invalid_graph = True
        leaves = sorted(set(ids) - {p for p in parents.values() if p in ids})
        selected, selection = (
            _selection(i, ids, leaves, i["_facts"], rejected)
            if ids
            else (None, dimension("unobserved", i["documents"]))
        )
        if (
            invalid_graph
            or i["flags"] & {"conflict", "parent_mismatch"}
            or any(
                attempts[aid]["flags"] & {"parent_mismatch", "conflict", "retry_cycle"}
                for aid in ids
            )
        ):
            selected = None
            selection = dimension("ambiguous", i["evidence"] | i["documents"])
        i["selection"] = selection
        i["current_attempt_id"] = selected
        current = attempts[selected] if selected else None
        if not ids:
            state = "unobserved"
        elif current is None:
            state = "ambiguous"
        else:
            execution = current["execution"]
            state = execution["state"]
            ancestors = set()
            parent = parents[selected]
            while parent in parents and parent not in ancestors:
                ancestors.add(parent)
                parent = parents[parent]
            had_zero = any(
                attempts[k]["execution"].get("exit_code") == 0
                and attempts[k]["execution"]["state"] == "exited"
                for k in ancestors
            )
            had_failure = any(
                attempts[k]["execution"]["state"] == "signaled"
                or attempts[k]["execution"].get("exit_code", 0) > 0
                for k in ancestors
            )
            if state == "exited":
                state = (
                    "recovered"
                    if execution["exit_code"] == 0 and had_failure
                    else "exited_zero"
                    if execution["exit_code"] == 0
                    else "failed"
                )
            elif state in ("started", "registered_only") and had_zero:
                state = "rerunning"
            if "orphan" in current["flags"] or "source_unknown" in current["flags"]:
                i["flags"].add("source_unknown")
            if "parent_mismatch" in i["flags"]:
                state = "ambiguous"
        i["execution"] = dimension(
            state,
            i["evidence"]
            | i["documents"]
            | {ref for aid in ids for ref in attempts[aid]["evidence"]},
        )
    for rid, r in runs.items():
        r["instance_ids"] = sorted(iid for iid, i in instances.items() if rid in i["_runs"])
        aids = sorted(aid for aid, a in attempts.items() if rid in a["_runs"])
        r["attempt_ids"] = aids
        declarations = _select(r["_facts"], "run.finish_declared")
        r["finish_declarations"] = [
            {"fact": event_ref(f), **f["event"]["payload"]}
            for f in sorted(declarations, key=event_ref)
        ]
        grouped = defaultdict(list)
        for f in declarations:
            p = f["event"]["payload"]
            grouped[p["scope_ref"], p["rule_version"]].append(f)
        finish_states = []
        for scope, versions in sorted(grouped.items()):
            by_declaration = defaultdict(set)
            for f in versions:
                by_declaration[f["event"]["payload"]["declaration_id"]].add(
                    canonical_hash(f["event"]["payload"])
                )
            maximal = [
                f
                for f in versions
                if not any(
                    set(f["event"]["payload"]["covered_attempt_ids"])
                    < set(g["event"]["payload"]["covered_attempt_ids"])
                    for g in versions
                )
            ]
            payloads = {
                (
                    tuple(f["event"]["payload"]["covered_attempt_ids"]),
                    f["event"]["payload"]["result"],
                )
                for f in maximal
            }
            if len(payloads) != 1 or any(len(v) > 1 for v in by_declaration.values()):
                state = "conflict"
                covered = []
            else:
                covered = list(next(iter(payloads))[0])
                foreign = [
                    aid for aid in covered if aid in attempts and rid not in attempts[aid]["_runs"]
                ]
                if foreign:
                    r["flags"].add("parent_mismatch")
                    rejected.extend(
                        {"fact": event_ref(f), "reason": "finish_parent_mismatch", "object_id": rid}
                        for f in maximal
                    )
                state = (
                    "conflict"
                    if foreign
                    else "needs_finalization"
                    if set(aids) - set(covered)
                    else "pending_association"
                    if set(covered) - set(aids)
                    else "finish_declared"
                )
            finish_states.append(
                {
                    "scope_ref": scope[0],
                    "rule_version": scope[1],
                    **dimension(state, _refs(versions)),
                    "covered_attempt_ids": covered,
                }
            )
        r["finish_scopes"] = finish_states
        states = {f["state"] for f in finish_states}
        registered = _select(r["_facts"], "run.registered")
        if not registered:
            r["flags"].add("source_unknown")
        lifecycle = (
            "conflict"
            if "conflict" in states or "conflict" in r["flags"]
            else "needs_finalization"
            if "needs_finalization" in states
            else "pending_association"
            if "pending_association" in states
            else "finish_declared"
            if states
            else "registered"
            if registered
            else "observed"
        )
        r["lifecycle"] = dimension(lifecycle, _refs(declarations + registered))
        r["engine_observations"] = [
            {"fact": event_ref(f), "event_type": f["event"]["event_type"], **f["event"]["payload"]}
            for f in sorted(r["_facts"], key=event_ref)
            if f["event"]["event_type"].startswith("engine.")
        ]
        r["product_observations"] = [
            {"fact": event_ref(f), "event_type": f["event"]["event_type"], **f["event"]["payload"]}
            for f in sorted(r["_facts"], key=event_ref)
            if f["event"]["event_type"].startswith(("artifact.", "delivery."))
        ]
        r["product_state"] = dimension(
            "unknown",
            _refs(
                [
                    f
                    for f in r["_facts"]
                    if f["event"]["event_type"].startswith(("artifact.", "delivery."))
                ]
            ),
            reason="versioned_product_rules_not_reduced",
        )
        for aid in aids:
            r["flags"].update(attempts[aid]["flags"])
        for iid in r["instance_ids"]:
            r["flags"].update(instances[iid]["flags"])
        for job in jobs.values():
            if rid in job["_runs"]:
                r["flags"].update(job["flags"])
    for h in health:
        for rid in h["run_ids"]:
            if rid in runs:
                runs[rid]["flags"].update(h["diagnostics"])
                runs[rid]["evidence"].add(h["id"])
    for kind, entities in maps.items():
        for n in entities.values():
            if kind == "instance" and not n["documents"]:
                n["flags"].add("orphan")
            flags = n["flags"]
            state = (
                "conflict"
                if flags & {"conflict", "parent_mismatch", "retry_cycle", "stage_conflict"}
                else "incomplete"
                if flags
                else "observed_scope"
            )
            n["observability"] = dimension(
                state, n["evidence"] | n["documents"], flags=sorted(flags)
            )
            n["run_ids"] = sorted(n["_runs"])
            if kind == "attempt":
                n["task_instance_ids"] = sorted(n["_instances"])
            if kind == "job":
                n["attempt_ids"] = sorted(n["_attempts"])
            for key in list(n):
                if key.startswith("_") or key == "flags":
                    del n[key]
            n["evidence"] = sorted(n["evidence"])
            n["documents"] = sorted(n["documents"])
            # Absence-based conclusions still identify the observed document/fact
            # scope that was inspected; do not rewrite payload or context objects.
            fallback = sorted(set(n["evidence"]) | set(n["documents"]))
            for field in (
                "registration",
                "execution",
                "selection",
                "lifecycle",
                "observability",
                "scheduler",
                "product_state",
            ):
                if field in n and not n[field]["because"]:
                    n[field]["because"] = fallback

    return {
        "runs": [runs[k] for k in sorted(runs)],
        "instances": [instances[k] for k in sorted(instances)],
        "attempts": [attempts[k] for k in sorted(attempts)],
        "jobs": [jobs[k] for k in sorted(jobs)],
        "facts": [
            {
                "ref": event_ref(f),
                "event_type": f["event"]["event_type"],
                "disposition": f["status"],
                "reason": f.get("reason"),
                "evidence_ids": sorted(
                    e["evidence_id"] for e in f["event"].get("evidence_refs", [])
                ),
            }
            for f in sorted(facts, key=event_ref)
        ],
        "rejected_facts": sorted(rejected, key=lambda r: (r["fact"], r["reason"])),
        "source_health": health,
    }
