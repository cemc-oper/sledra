"""Persistent parent inputs and immutable projection revisions in the raw DB's snapshot."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from sledra_analysis.ingest.validation import EventValidator
from sledra_analysis.projections.documents import DocumentRecord, DocumentValidator, KINDS
from sledra_analysis.projections.reducer import REDUCER_VERSION, RULE_VERSION, reconstruct
from sledra_analysis.snapshot import (
    ProjectionError,
    contract_hashes,
    ensure_projection_schema,
    read_inputs,
    read_watermark,
)
from sledra_analysis.store.canonical import canonical_bytes, canonical_hash, parse_json
from sledra_analysis.store.raw import RawStore, _timestamp


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
        self.contract_hashes = contract_hashes()
        ensure_projection_schema(store)

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

    def rebuild(self, *, published_at: str) -> ProjectionRevision:
        if not _timestamp(published_at):
            raise ProjectionError("projection publication needs timezone-aware time")
        with self.store.transaction():
            db = self.store._db()
            watermark = read_watermark(db)
            facts, documents, health, rejections = read_inputs(
                db,
                self.validator,
                self.document_validator,
                max_facts=self.max_facts,
                max_documents=self.max_documents,
                max_input_bytes=self.max_input_bytes,
            )
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
        current = read_watermark(self.store._db())
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
