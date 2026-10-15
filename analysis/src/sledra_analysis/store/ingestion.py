"""Atomic source positions, append-only reconstruction work and commit watermarks."""

from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import asdict

from sledra_analysis.ingest.source import Checkpoint, SourceKey

INGESTION_SCHEMA = """
CREATE TABLE ingestion_identity (
    lineage TEXT PRIMARY KEY
) STRICT;
INSERT INTO ingestion_identity VALUES(lower(hex(randomblob(16))));
CREATE TABLE ingestion_commits (
    commit_id INTEGER PRIMARY KEY,
    collected_at TEXT NOT NULL
) STRICT;
CREATE TABLE checkpoints (
    source_root_id TEXT NOT NULL, source_id TEXT NOT NULL, segment_uuid TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK(generation >= 0),
    committed_offset INTEGER NOT NULL CHECK(committed_offset >= 0),
    state TEXT NOT NULL,
    diagnostics TEXT NOT NULL,
    commit_id INTEGER NOT NULL REFERENCES ingestion_commits(commit_id),
    PRIMARY KEY(source_root_id, source_id, segment_uuid, generation)
) STRICT;
CREATE TABLE registry_checkpoints (
    source_root_id TEXT NOT NULL, source_id TEXT NOT NULL, locator TEXT NOT NULL,
    raw_hash TEXT NOT NULL, byte_size INTEGER NOT NULL,
    commit_id INTEGER NOT NULL REFERENCES ingestion_commits(commit_id),
    PRIMARY KEY(source_root_id, source_id, locator, raw_hash)
) STRICT;
CREATE TABLE projection_work_queue (
    work_id INTEGER PRIMARY KEY,
    object_kind TEXT NOT NULL CHECK(object_kind IN ('event', 'quarantine', 'validation', 'source')),
    object_key TEXT NOT NULL, revision_key TEXT NOT NULL,
    UNIQUE(object_kind, object_key, revision_key)
) STRICT;
CREATE TRIGGER enqueue_event AFTER INSERT ON event_versions BEGIN
    INSERT OR IGNORE INTO projection_work_queue(object_kind, object_key, revision_key)
    VALUES('event', NEW.event_id, NEW.canonical_hash);
END;
CREATE TRIGGER enqueue_quarantine AFTER INSERT ON quarantine BEGIN
    INSERT OR IGNORE INTO projection_work_queue(object_kind, object_key, revision_key)
    VALUES('quarantine', CAST(NEW.quarantine_id AS TEXT), NEW.validator_version);
END;
CREATE TRIGGER enqueue_validation AFTER INSERT ON event_validations BEGIN
    INSERT OR IGNORE INTO projection_work_queue(object_kind, object_key, revision_key)
    VALUES('validation', CAST(NEW.raw_record_id AS TEXT), NEW.validator_version);
END;
INSERT INTO projection_work_queue(object_kind, object_key, revision_key)
SELECT 'event', event_id, canonical_hash FROM event_versions;
INSERT INTO projection_work_queue(object_kind, object_key, revision_key)
SELECT 'quarantine', CAST(quarantine_id AS TEXT), validator_version FROM quarantine;
INSERT INTO projection_work_queue(object_kind, object_key, revision_key)
SELECT 'validation', CAST(raw_record_id AS TEXT), validator_version FROM event_validations;
CREATE TABLE raw_archives (
    source_root_id TEXT NOT NULL, source_id TEXT NOT NULL, segment_uuid TEXT NOT NULL,
    generation INTEGER NOT NULL,
    raw_hash TEXT NOT NULL, byte_size INTEGER NOT NULL, archive_locator TEXT NOT NULL,
    commit_id INTEGER NOT NULL REFERENCES ingestion_commits(commit_id),
    PRIMARY KEY(source_root_id, source_id, segment_uuid, generation),
    FOREIGN KEY(source_root_id, source_id, segment_uuid, generation)
      REFERENCES checkpoints(source_root_id, source_id, segment_uuid, generation)
) STRICT;
"""


def encode_checkpoint(state: Checkpoint) -> str:
    return json.dumps(asdict(state), sort_keys=True, separators=(",", ":"))


def decode_checkpoint(value: str) -> Checkpoint:
    state = json.loads(value)
    state["key"] = SourceKey(**state["key"])
    state["discovered_paths"] = tuple(state["discovered_paths"])
    return Checkpoint(**state)


def checkpoint_key(state: Checkpoint) -> tuple:
    key = state.key
    return (key.source_root_id, key.source_id, key.segment_uuid, state.generation)


class IngestionMixin:
    @contextmanager
    def _write_scope(self):
        # Only the ingestion coordinator may join its outer transaction. Public
        # transaction() still rejects arbitrary nesting and never silently commits it.
        if self._joining_ingest:
            if not self._active:
                raise RuntimeError("ingestion write requires its transaction")
            yield self
        else:
            with self.transaction():
                yield self

    @contextmanager
    def _ingestion_transaction(self, collected_at: str):
        from sledra_analysis.store.raw import RecordError, _timestamp

        if not _timestamp(collected_at):
            raise RecordError("invalid collection time")
        with self.transaction():
            self._joining_ingest = True
            try:
                commit = (
                    self._db()
                    .execute(
                        "INSERT INTO ingestion_commits(collected_at) VALUES(?)", (collected_at,)
                    )
                    .lastrowid
                )
                yield commit
            finally:
                self._joining_ingest = False

    def checkpoints(self, source_root_id: str) -> tuple[Checkpoint, ...]:
        return tuple(
            decode_checkpoint(row[0])
            for row in self._db().execute(
                "SELECT state FROM checkpoints WHERE source_root_id = ? "
                "ORDER BY source_id, segment_uuid, generation",
                (source_root_id,),
            )
        )

    def _save_checkpoint(self, state: Checkpoint, diagnostics: tuple[str, ...], commit: int):
        from sledra_analysis.store.raw import StoreError

        if not self._joining_ingest:
            raise StoreError("checkpoint requires an ingestion transaction")
        key = checkpoint_key(state)
        previous = (
            self._db()
            .execute(
                "SELECT state, diagnostics FROM checkpoints WHERE source_root_id=? AND source_id=? "
                "AND segment_uuid=? AND generation=?",
                key,
            )
            .fetchone()
        )
        if previous:
            diagnostics = tuple(dict.fromkeys((*json.loads(previous[1]), *diagnostics)))
        old = decode_checkpoint(previous[0]) if previous else None
        floor = old.committed_offset if old else 0
        if state.committed_offset < floor or (state.conflict and state.committed_offset != floor):
            raise StoreError("checkpoint regressed or conflicted source advanced")
        # Independently prove that every new byte after the verified native header
        # is covered by a persisted raw or quarantine range in this transaction.
        offset = max(floor, state.header_size if state.protocol_version else 0)
        ranges = self._db().execute(
            "SELECT DISTINCT l.byte_start, l.byte_end FROM source_locations l "
            "WHERE l.source_root_id=? AND l.source_id=? AND l.segment_uuid=? AND l.generation=? "
            "AND l.byte_end > ? AND l.byte_end <= ? AND ("
            "EXISTS(SELECT 1 FROM raw_records r WHERE r.location_id=l.location_id) OR "
            "EXISTS(SELECT 1 FROM quarantine q WHERE q.location_id=l.location_id)) "
            "ORDER BY l.byte_start, l.byte_end",
            (*key, offset, state.committed_offset),
        )
        for start, end in ranges:
            if start > offset:
                raise StoreError("checkpoint would skip unpreserved bytes")
            offset = max(offset, end)
        if state.committed_offset > offset:
            raise StoreError("checkpoint exceeds preserved bytes")
        self._db().execute(
            "INSERT INTO checkpoints VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(source_root_id, source_id, segment_uuid, generation) DO UPDATE SET "
            "committed_offset=excluded.committed_offset, state=excluded.state, "
            "diagnostics=excluded.diagnostics, commit_id=excluded.commit_id",
            (
                *key,
                state.committed_offset,
                encode_checkpoint(state),
                json.dumps(diagnostics),
                commit,
            ),
        )

        # Source gaps/conflicts and partial-tail growth also invalidate health
        # projections, even if this scan produced no new business event.
        revision = {
            "offset": state.committed_offset,
            "size": state.last_size,
            "header": state.header_hash,
            "anchor": state.anchor_hash,
            "sealed_hash": state.sealed_hash,
            "conflict": state.conflict,
            "diagnostics": diagnostics,
        }
        digest = hashlib.sha256(json.dumps(revision, sort_keys=True).encode()).hexdigest()
        self._db().execute(
            "INSERT INTO projection_work_queue(object_kind, object_key, revision_key) "
            "VALUES('source', ?, ?) ON CONFLICT DO NOTHING",
            (json.dumps(key), digest),
        )

    def projection_work(self, *, after_id: int = 0, limit: int = 1000) -> tuple[tuple, ...]:
        if (
            type(after_id) is not int
            or after_id < 0
            or type(limit) is not int
            or not 0 < limit <= 10000
        ):
            raise ValueError("invalid work queue page")
        return tuple(
            self._db().execute(
                "SELECT work_id, object_kind, object_key, revision_key FROM projection_work_queue "
                "WHERE work_id > ? ORDER BY work_id LIMIT ?",
                (after_id, limit),
            )
        )

    def watermark(self) -> dict:
        from sledra_analysis.store.raw import StoreError

        if self._active:
            raise StoreError("watermark requires committed state")
        return {
            "lineage": self._db().execute("SELECT lineage FROM ingestion_identity").fetchone()[0],
            "commit_id": self._db()
            .execute("SELECT coalesce(max(commit_id), 0) FROM ingestion_commits")
            .fetchone()[0],
            "queue_id": self._db()
            .execute("SELECT coalesce(max(work_id), 0) FROM projection_work_queue")
            .fetchone()[0],
        }
