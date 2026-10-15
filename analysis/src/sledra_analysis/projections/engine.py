"""Persistent parent inputs and immutable projection revisions in the raw DB's snapshot."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.resources import files
from typing import Iterable

from sledra_analysis.ingest.validation import EventValidator
from sledra_analysis.projections.documents import DocumentRecord, DocumentValidator, KINDS
from sledra_analysis.projections.reducer import REDUCER_VERSION, RULE_VERSION, reconstruct
from sledra_analysis.store.canonical import RecordError, canonical_bytes, canonical_hash, parse_json
from sledra_analysis.store.raw import RawStore, StoreError, _timestamp

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
    """Reconstruction cannot be proven; keep the previous revision and work cursor."""


@dataclass(frozen=True)
class ProjectionRevision:
    revision_id: str
    reducer_version: str
    rule_version: str
    input_hash: str
    data: dict
    published_at: str
    watermark: dict


class ProjectionEngine:
    """Full deterministic rebuild with explicit local budgets; single management writer."""

    def __init__(
        self,
        store: RawStore,
        *,
        rule_version: str = RULE_VERSION,
        max_facts: int = 50000,
        max_documents: int = 10000,
        max_input_bytes: int = 64 * 1024 * 1024,
        max_output_bytes: int = 64 * 1024 * 1024,
    ):
        if rule_version != RULE_VERSION:
            raise ProjectionError("unsupported state rule version")
        if any(
            type(v) is not int or v <= 0
            for v in (max_facts, max_documents, max_input_bytes, max_output_bytes)
        ):
            raise ValueError("projection budgets must be positive integers")
        self.store, self.rule_version = store, rule_version
        self.max_facts, self.max_documents = max_facts, max_documents
        self.max_input_bytes, self.max_output_bytes = max_input_bytes, max_output_bytes
        self.validator, self.document_validator = EventValidator(), DocumentValidator()
        resources = files("sledra_analysis").joinpath("resources/contract")
        self.contract_hashes = {
            name: hashlib.sha256(resources.joinpath(name).read_bytes()).hexdigest()
            for name in sorted(
                ["event.schema.json", "event-types.json", *[k + ".schema.json" for k in KINDS]]
            )
        }
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

    def _stage(self, name: str) -> None:
        """Internal failure-injection seam."""

    def import_documents(self, records: Iterable[DocumentRecord]) -> int:
        prepared = []
        total = 0
        for record in records:
            if (
                not isinstance(record.raw_bytes, bytes)
                or not 0 < len(record.raw_bytes) <= self.store.max_record_bytes
                or not isinstance(record.locator, str)
                or not record.locator
                or not _timestamp(record.collected_at)
            ):
                raise ProjectionError("invalid document record or byte budget")
            total += len(record.raw_bytes)
            if len(prepared) >= self.max_documents or total > self.max_input_bytes:
                raise ProjectionError("document import exceeds batch budget")
            doc = self.document_validator.validate(record.raw_bytes, record.pointer)
            encoded = canonical_bytes(doc)
            total += max(0, len(encoded) - len(record.raw_bytes))
            if len(encoded) > self.store.max_record_bytes or total > self.max_input_bytes:
                raise ProjectionError("canonical document exceeds import budget")
            prepared.append((record, doc, encoded, hashlib.sha256(encoded).hexdigest()))
        added = 0
        with self.store.transaction():
            db = self.store._db()
            for record, doc, encoded, digest in prepared:
                key = (doc["kind"], doc[KINDS[doc["kind"]]], digest)
                added += db.execute(
                    "INSERT INTO projection_documents VALUES(?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (*key, encoded),
                ).rowcount
                if (
                    db.execute(
                        "SELECT canonical_bytes FROM projection_documents WHERE kind=? AND object_id=? AND canonical_hash=?",
                        key,
                    ).fetchone()[0]
                    != encoded
                ):
                    raise ProjectionError("parent document hash collision")
                db.execute(
                    "INSERT INTO projection_document_sources VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (
                        *key,
                        hashlib.sha256(record.raw_bytes).hexdigest(),
                        record.raw_bytes,
                        record.locator,
                        record.pointer,
                        record.collected_at,
                    ),
                )
                preserved = db.execute(
                    "SELECT raw_bytes FROM projection_document_sources WHERE kind=? AND object_id=? AND canonical_hash=? AND raw_hash=? AND locator=? AND pointer=?",
                    (
                        *key,
                        hashlib.sha256(record.raw_bytes).hexdigest(),
                        record.locator,
                        record.pointer,
                    ),
                ).fetchone()[0]
                if preserved != record.raw_bytes:
                    raise ProjectionError("parent source hash collision")
                db.execute(
                    "INSERT INTO projection_work_queue(object_kind,object_key,revision_key) VALUES('source', ?, ?) ON CONFLICT DO NOTHING",
                    (json.dumps(["document", *key[:2]]), digest),
                )
            self._stage("after_document_import")
        return added

    def _watermark(self):
        db = self.store._db()
        return {
            "lineage": db.execute("SELECT lineage FROM ingestion_identity").fetchone()[0],
            "commit_id": db.execute(
                "SELECT coalesce(max(commit_id),0) FROM ingestion_commits"
            ).fetchone()[0],
            "queue_id": db.execute(
                "SELECT coalesce(max(work_id),0) FROM projection_work_queue"
            ).fetchone()[0],
        }

    def _inputs(self):
        db = self.store._db()
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
        source_count = db.execute("SELECT count(*) FROM raw_records").fetchone()[0]
        quarantine_count = db.execute("SELECT count(*) FROM quarantine").fetchone()[0]
        if (
            source_count > self.max_facts * 10
            or quarantine_count > self.max_facts * 10
            or count > self.max_facts
            or dc + rc > self.max_documents
            or hc > self.max_facts
            or size + ds + rs > self.max_input_bytes
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
                validated = self.validator.validate(raw)
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
                doc = self.document_validator.validate(raw)
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
                doc = self.document_validator.validate(raw, "/context")
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
                        "run_context_sha256"
                        if expected == "run_context"
                        else "attempt_context_sha256"
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
            "SELECT DISTINCT l.source_root_id,l.source_id,l.segment_uuid,l.generation,r.event_id,r.canonical_hash FROM raw_records r JOIN source_locations l USING(location_id)"
        ):
            rid = associations.get((eid, digest))
            if rid is not None:
                source_runs.setdefault((root, sid, segment, generation), set()).add(rid)
        quarantines = {
            tuple(row[:4]): row[4]
            for row in db.execute(
                "SELECT l.source_root_id,l.source_id,l.segment_uuid,l.generation,count(*) FROM quarantine q JOIN source_locations l USING(location_id) GROUP BY l.source_root_id,l.source_id,l.segment_uuid,l.generation"
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
                    "historical_diagnostics": sorted(history),
                    "quarantine_count": quarantines.get(key, 0),
                    "committed_offset": state["committed_offset"],
                    "observed_size": state["last_size"],
                    "sealed_hash": state["sealed_hash"],
                }
            )
        return facts, documents, health, rejections

    def rebuild(self, *, published_at: str) -> ProjectionRevision:
        if not _timestamp(published_at):
            raise ProjectionError("projection publication needs timezone-aware time")
        with self.store.transaction():
            watermark = self._watermark()
            facts, documents, health, rejections = self._inputs()
            material = {
                "facts": facts,
                "documents": documents,
                "health": health,
                "document_rejections": rejections,
                "contracts": self.contract_hashes,
                "validator": self.validator.version,
            }
            input_hash = canonical_hash(material)
            revision_id = "prj_" + canonical_hash(
                {"reducer": REDUCER_VERSION, "rules": self.rule_version, "input": input_hash}
            )
            self._stage("after_input_snapshot")
            state = reconstruct(facts, documents, health)
            state["document_rejections"] = rejections
            state["input_documents"] = [
                {"kind": d["kind"], "id": d[KINDS[d["kind"]]], "canonical_hash": canonical_hash(d)}
                for d in documents
            ]
            encoded = canonical_bytes(state)
            if len(encoded) > self.max_output_bytes:
                raise ProjectionError("projection output exceeds configured budget")
            db = self.store._db()
            db.execute(
                "INSERT INTO projection_revisions VALUES(?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (
                    revision_id,
                    REDUCER_VERSION,
                    self.rule_version,
                    input_hash,
                    encoded,
                    published_at,
                    json.dumps(watermark, sort_keys=True),
                ),
            )
            if (
                db.execute(
                    "SELECT state_bytes FROM projection_revisions WHERE revision_id=?",
                    (revision_id,),
                ).fetchone()[0]
                != encoded
            ):
                raise ProjectionError("projection revision collision or nondeterministic reducer")
            self._stage("after_revision")
            db.execute(
                "INSERT INTO projection_heads VALUES(?, ?, ?, ?) ON CONFLICT(reducer_version,rule_version) DO UPDATE SET revision_id=excluded.revision_id,watermark=excluded.watermark",
                (
                    REDUCER_VERSION,
                    self.rule_version,
                    revision_id,
                    json.dumps(watermark, sort_keys=True),
                ),
            )
            self._stage("after_work_cursor")
            self._stage("before_projection_commit")
        self._stage("after_projection_commit")
        return self.revision(revision_id)

    def revision(self, revision_id: str | None = None) -> ProjectionRevision | None:
        db = self.store._db()
        if revision_id is None:
            head = db.execute(
                "SELECT revision_id FROM projection_heads WHERE reducer_version=? AND rule_version=?",
                (REDUCER_VERSION, self.rule_version),
            ).fetchone()
            if head is None:
                return None
            revision_id = head[0]
        row = db.execute(
            "SELECT revision_id,reducer_version,rule_version,input_hash,state_bytes,published_at,watermark FROM projection_revisions WHERE revision_id=?",
            (revision_id,),
        ).fetchone()
        return (
            ProjectionRevision(*row[:4], parse_json(row[4]), row[5], json.loads(row[6]))
            if row
            else None
        )

    def status(self) -> dict:
        if self.store._active:
            raise ProjectionError("projection status requires committed state")
        current = self._watermark()
        head = (
            self.store._db()
            .execute(
                "SELECT revision_id,watermark FROM projection_heads WHERE reducer_version=? AND rule_version=?",
                (REDUCER_VERSION, self.rule_version),
            )
            .fetchone()
        )
        consumed = json.loads(head[1]) if head else None
        return {
            "revision_id": head[0] if head else None,
            "state": "pending"
            if consumed is None
            or consumed["lineage"] != current["lineage"]
            or consumed["queue_id"] < current["queue_id"]
            else "current",
            "source_watermark": current,
            "projection_watermark": consumed,
        }

    def get(self, kind: str, identity: str, *, revision_id: str | None = None) -> dict | None:
        if kind not in ("runs", "instances", "attempts", "jobs"):
            raise ValueError("unknown projection kind")
        revision = self.revision(revision_id)
        return (
            next((item for item in revision.data[kind] if item["id"] == identity), None)
            if revision
            else None
        )
