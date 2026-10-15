"""Read one semantic fact/document/source snapshot from the caller's SQLite transaction."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files

from sledra_analysis.projections.documents import KINDS
from sledra_analysis.store.canonical import RecordError, canonical_hash, parse_json
from sledra_analysis.store.raw import StoreError


SCHEMA = """
CREATE TABLE projection_metadata(version INTEGER PRIMARY KEY CHECK(version=1)) STRICT;
INSERT INTO projection_metadata VALUES(1);
CREATE TABLE projection_documents (
    kind TEXT NOT NULL, object_id TEXT NOT NULL, canonical_hash TEXT NOT NULL,
    canonical_bytes BLOB NOT NULL,
    PRIMARY KEY(kind, object_id, canonical_hash)
) STRICT;
CREATE TABLE projection_document_sources (
    kind TEXT NOT NULL, object_id TEXT NOT NULL, canonical_hash TEXT NOT NULL,
    raw_hash TEXT NOT NULL, raw_bytes BLOB NOT NULL, locator TEXT NOT NULL,
    pointer TEXT NOT NULL, collected_at TEXT NOT NULL,
    PRIMARY KEY(kind, object_id, canonical_hash, raw_hash, locator, pointer),
    FOREIGN KEY(kind, object_id, canonical_hash)
      REFERENCES projection_documents(kind, object_id, canonical_hash)
) STRICT;
CREATE TABLE projection_revisions (
    revision_id TEXT PRIMARY KEY,
    reducer_version TEXT NOT NULL, rule_version TEXT NOT NULL,
    input_hash TEXT NOT NULL, state_bytes BLOB NOT NULL,
    published_at TEXT NOT NULL, watermark TEXT NOT NULL,
    UNIQUE(reducer_version, rule_version, input_hash)
) STRICT;
CREATE TABLE projection_heads (
    reducer_version TEXT NOT NULL, rule_version TEXT NOT NULL,
    revision_id TEXT NOT NULL REFERENCES projection_revisions(revision_id),
    watermark TEXT NOT NULL,
    PRIMARY KEY(reducer_version, rule_version)
) STRICT;
"""


class ProjectionError(StoreError):
    """Reconstruction cannot be proven; keep the previous published head."""


def ensure_projection_schema(store):
    connection = store._db()
    if store._active:
        raise ProjectionError("projection initialization requires committed state")
    metadata = connection.execute(
        "SELECT name FROM sqlite_master WHERE name='projection_metadata'"
    ).fetchone()
    if metadata:
        if connection.execute("SELECT version FROM projection_metadata").fetchall() != [(1,)]:
            raise ProjectionError("unsupported projection schema")
    else:
        try:
            connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA)
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise


def contract_hashes():
    resources = files("sledra_analysis").joinpath("resources/contract")
    return {
        name: hashlib.sha256(resources.joinpath(name).read_bytes()).hexdigest()
        for name in sorted(
            ["event.schema.json", "event-types.json", *[k + ".schema.json" for k in KINDS]]
        )
    }


def read_watermark(db):
    return {
        "lineage": db.execute("SELECT lineage FROM ingestion_identity").fetchone()[0],
        "commit_id": db.execute(
            "SELECT coalesce(max(commit_id),0) FROM ingestion_commits"
        ).fetchone()[0],
        "queue_id": db.execute(
            "SELECT coalesce(max(work_id),0) FROM projection_work_queue"
        ).fetchone()[0],
    }


def read_inputs(db, validator, document_validator, *, max_facts, max_documents, max_input_bytes):
    count, size = db.execute(
        "SELECT count(*),coalesce(sum(length(canonical_bytes)),0) FROM event_versions"
    ).fetchone()
    dc, ds = db.execute(
        "SELECT count(*),coalesce(sum(length(canonical_bytes)),0) FROM projection_documents"
    ).fetchone()
    rc, rs = db.execute(
        "SELECT count(*),coalesce(sum(length(raw_bytes)),0) FROM raw_blobs WHERE raw_hash IN (SELECT raw_hash FROM raw_records WHERE event_pointer='/outbox_event')"
    ).fetchone()
    hc = db.execute("SELECT count(*) FROM checkpoints").fetchone()[0]
    registry_count = db.execute("SELECT count(*) FROM registry_checkpoints").fetchone()[0]
    source_count = db.execute("SELECT count(*) FROM raw_records").fetchone()[0]
    quarantine_count = db.execute("SELECT count(*) FROM quarantine").fetchone()[0]
    if (
        source_count > max_facts * 10
        or quarantine_count > max_facts * 10
        or count > max_facts
        or dc + rc > max_documents
        or hc + registry_count > max_facts
        or size + ds + rs > max_input_bytes
    ):
        raise ProjectionError("projection inputs exceed configured rebuild budget")
    facts = []
    associations = {}
    groups = {}
    for identity, digest, raw in db.execute(
        "SELECT event_id,canonical_hash,canonical_bytes FROM event_versions ORDER BY event_id,canonical_hash"
    ):
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ProjectionError("stored canonical event digest mismatch")
        e = parse_json(raw)
        try:
            validated = validator.validate(raw)
            status = "known" if validated.supported else "opaque"
            reason = None
        except RecordError as exc:
            status, reason = "invalid", exc.code
        facts.append({"event": e, "hash": digest, "status": status, "reason": reason})
        groups[identity] = groups.get(identity, 0) + 1
        rid = e["association"].get("run_id")
        if isinstance(rid, str):
            associations[identity, digest] = rid
    for f in facts:
        if groups[f["event"]["event_id"]] > 1:
            f["status"] = "conflict"
            f["reason"] = "conflicting_event_id"
    documents = []
    for kind, identity, digest, raw in db.execute(
        "SELECT kind,object_id,canonical_hash,canonical_bytes FROM projection_documents ORDER BY kind,object_id,canonical_hash"
    ):
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ProjectionError("stored parent document digest mismatch")
        try:
            doc = document_validator.validate(raw)
        except RecordError as exc:
            raise ProjectionError("stored parent document no longer validates") from exc
        if doc["kind"] != kind or doc[KINDS[kind]] != identity:
            raise ProjectionError("stored parent document identity mismatch")
        documents.append(doc)
    rejections = []
    for raw_hash, raw in db.execute(
        "SELECT raw_hash,raw_bytes FROM raw_blobs WHERE raw_hash IN (SELECT raw_hash FROM raw_records WHERE event_pointer='/outbox_event') ORDER BY raw_hash"
    ):
        if hashlib.sha256(raw).hexdigest() != raw_hash:
            raise ProjectionError("stored Registry source digest mismatch")
        wrapper = parse_json(raw)
        if not isinstance(wrapper, dict) or "context" not in wrapper:
            continue
        try:
            doc = document_validator.validate(raw, "/context")
            e = wrapper["outbox_event"]
            a = e["association"]
            p = e["payload"]
            expected = (
                "run_context"
                if e["event_type"] == "run.registered"
                else "attempt_context"
                if e["event_type"] == "attempt.registered"
                else None
            )
            if (
                doc["kind"] != expected
                or doc["run_id"] != a.get("run_id")
                or doc["context_sha256"]
                != p.get(
                    "run_context_sha256" if expected == "run_context" else "attempt_context_sha256"
                )
                or (
                    expected == "attempt_context"
                    and (
                        doc["attempt_id"] != a.get("attempt_id")
                        or doc["task_instance_id"] != a.get("task_instance_id")
                    )
                )
            ):
                raise RecordError("wrapper parent mismatch", "parent_mismatch")
            documents.append(doc)
        except RecordError as exc:
            rejections.append({"raw_hash": raw_hash, "reason": exc.code})
    # Canonical duplicates (standalone copy vs Registry context) never multiply entities.
    documents = list({canonical_hash(doc): doc for doc in documents}.values())
    documents.sort(key=lambda d: (d["kind"], d[KINDS[d["kind"]]], canonical_hash(d)))
    source_runs = {}
    for root, sid, segment, generation, eid, digest in db.execute(
        "SELECT DISTINCT l.source_root_id,l.source_id,l.segment_uuid,l.generation,r.event_id,r.canonical_hash FROM raw_records r JOIN source_locations l USING(location_id) WHERE l.source_kind='segment'"
    ):
        rid = associations.get((eid, digest))
        if rid is not None:
            source_runs.setdefault((root, sid, segment, generation), set()).add(rid)
    quarantines = {
        tuple(row[:4]): row[4]
        for row in db.execute(
            "SELECT l.source_root_id,l.source_id,l.segment_uuid,l.generation,count(*) FROM quarantine q JOIN source_locations l USING(location_id) WHERE l.source_kind='segment' GROUP BY l.source_root_id,l.source_id,l.segment_uuid,l.generation"
        )
    }
    health = []
    transient = {
        "open_partial_tail",
        "stuck_tail",
        "scan_partial_record",
        "scan_byte_budget_reached",
        "scan_record_budget_reached",
    }
    for root, sid, segment, generation, state, diagnostics in db.execute(
        "SELECT source_root_id,source_id,segment_uuid,generation,state,diagnostics FROM checkpoints ORDER BY source_root_id,source_id,segment_uuid,generation"
    ):
        state = json.loads(state)
        history = set(json.loads(diagnostics))
        flags = history - transient
        key = (root, sid, segment, generation)
        if state["committed_offset"] < state["last_size"]:
            flags.add("pending_source_bytes")
        if state["conflict"]:
            flags.add("segment_uuid_conflict")
        if quarantines.get(key, 0):
            flags.add("quarantined_records")
        health.append(
            {
                "id": "source:" + canonical_hash(list(key)),
                "source_key": list(key),
                "run_ids": sorted(source_runs.get(key, ())),
                "diagnostics": sorted(flags),
                "quarantine_count": quarantines.get(key, 0),
                "committed_offset": state["committed_offset"],
                "observed_size": state["last_size"],
                "sealed_hash": state["sealed_hash"],
            }
        )
    registry = {}

    def registry_entry(key):
        return registry.setdefault(
            key,
            {
                "raw_hashes": set(),
                "quarantine_hashes": set(),
                "quarantine_count": 0,
                "run_ids": set(),
            },
        )

    for root, sid, locator, raw_hash in db.execute(
        "SELECT source_root_id,source_id,locator,raw_hash FROM registry_checkpoints ORDER BY source_root_id,source_id,locator,raw_hash"
    ):
        registry_entry((root, sid, locator))["raw_hashes"].add(raw_hash)
    for root, sid, locator, raw_hash in db.execute(
        "SELECT l.source_root_id,l.source_id,l.locator,b.raw_hash FROM quarantine q JOIN source_locations l USING(location_id) JOIN quarantine_blobs b USING(blob_id) WHERE l.source_kind='registry' ORDER BY l.source_root_id,l.source_id,l.locator,b.raw_hash"
    ):
        entry = registry_entry((root, sid, locator))
        entry["quarantine_hashes"].add(raw_hash)
        entry["quarantine_count"] += 1
    for root, sid, locator, eid, digest in db.execute(
        "SELECT DISTINCT l.source_root_id,l.source_id,l.locator,r.event_id,r.canonical_hash FROM raw_records r JOIN source_locations l USING(location_id) WHERE l.source_kind='registry'"
    ):
        rid = associations.get((eid, digest))
        if rid is not None:
            registry_entry((root, sid, locator))["run_ids"].add(rid)
    for key, value in sorted(registry.items()):
        raw_hashes = sorted(value["raw_hashes"])
        isolated = sorted(value["quarantine_hashes"])
        flags = []
        if len(raw_hashes) > 1:
            flags.append("registry_immutable_conflict")
        if isolated:
            flags.append("quarantined_records")
        health.append(
            {
                "id": "registry:" + canonical_hash(list(key)),
                "source_key": [key[0], key[1], "registry:" + canonical_hash(key[2])],
                "run_ids": sorted(value["run_ids"]),
                "diagnostics": flags,
                "quarantine_count": value["quarantine_count"],
                "raw_hashes": raw_hashes,
                "quarantine_hashes": isolated,
            }
        )
    return facts, documents, health, rejections
