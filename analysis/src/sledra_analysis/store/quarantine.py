"""Streamed quarantine BLOBs, immutable reasons and audited reprocessing."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO, Iterator

from sledra_analysis.store.canonical import RecordError

if TYPE_CHECKING:
    from sledra_analysis.ingest.validation import EventValidator
    from sledra_analysis.store.raw import SourceLocation

QUARANTINE_SCHEMA = """
CREATE TABLE quarantine_blobs (
    blob_id INTEGER PRIMARY KEY,
    raw_hash TEXT NOT NULL UNIQUE CHECK(length(raw_hash) = 64),
    byte_size INTEGER NOT NULL CHECK(byte_size > 0),
    raw_bytes BLOB NOT NULL CHECK(length(raw_bytes) = byte_size)
) STRICT;
CREATE TABLE quarantine (
    quarantine_id INTEGER PRIMARY KEY,
    location_id INTEGER NOT NULL REFERENCES source_locations(location_id),
    blob_id INTEGER NOT NULL REFERENCES quarantine_blobs(blob_id),
    reason TEXT NOT NULL,
    validator_version TEXT NOT NULL,
    collected_at TEXT NOT NULL,
    event_pointer TEXT NOT NULL CHECK(event_pointer IN ('', '/outbox_event')),
    UNIQUE(location_id, blob_id, reason, validator_version, event_pointer)
) STRICT;
CREATE TABLE event_validations (
    raw_record_id INTEGER NOT NULL REFERENCES raw_records(raw_record_id),
    validator_version TEXT NOT NULL,
    semantic_support TEXT NOT NULL CHECK(semantic_support IN ('known', 'opaque')),
    PRIMARY KEY(raw_record_id, validator_version)
) STRICT;
CREATE TABLE quarantine_replays (
    replay_id INTEGER PRIMARY KEY,
    quarantine_id INTEGER NOT NULL REFERENCES quarantine(quarantine_id),
    validator_version TEXT NOT NULL,
    attempted_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('retained', 'accepted', 'opaque')),
    reason TEXT NOT NULL,
    event_id TEXT,
    canonical_hash TEXT,
    FOREIGN KEY(event_id, canonical_hash) REFERENCES event_versions(event_id, canonical_hash),
    CHECK((status = 'retained' AND event_id IS NULL AND canonical_hash IS NULL)
       OR (status != 'retained' AND event_id IS NOT NULL AND canonical_hash IS NOT NULL))
) STRICT;
"""


class QuarantineError(RuntimeError):
    """Isolation failed; input must be retried without advancing a position."""


class QuarantineLimit(QuarantineError):
    """Isolation capacity cannot preserve this input."""


@dataclass(frozen=True)
class QuarantinedRecord:
    quarantine_id: int
    location: SourceLocation
    raw_hash: str
    byte_size: int
    reason: str
    validator_version: str
    collected_at: str
    event_pointer: str


@dataclass(frozen=True)
class ReplayResult:
    replay_id: int
    quarantine_id: int
    validator_version: str
    attempted_at: str
    status: str
    reason: str
    event_id: str | None
    canonical_hash: str | None


def _metadata(reason: str, version: str) -> None:
    if not isinstance(reason, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", reason):
        raise QuarantineError("reason must be a bounded machine code")
    if not isinstance(version, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,95}", version
    ):
        raise QuarantineError("validator version must be explicit and bounded")


def _read(stream: BinaryIO, size: int) -> bytes:
    chunks = []
    while size:
        block = stream.read(size)
        if not block:
            raise QuarantineError("short isolation stream")
        if len(block) > size:
            raise QuarantineError("isolation stream exceeded requested chunk")
        chunks.append(block)
        size -= len(block)
    return b"".join(chunks)


class QuarantineMixin:
    """Uses RawStore's connection and transaction; never writes a source checkpoint."""

    def _mark_validation(self, record, digest: str, version: str, supported: bool) -> None:
        _metadata("known" if supported else "unsupported_semantics", version)
        connection = self._db()
        location_id = self._location_id(record.location)
        raw_hash = hashlib.sha256(record.raw_bytes).hexdigest()
        record_id = connection.execute(
            "SELECT raw_record_id FROM raw_records WHERE location_id = ? AND raw_hash = ? "
            "AND event_pointer = ? AND canonical_hash = ?",
            (location_id, raw_hash, record.event_pointer, digest),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO event_validations VALUES(?, ?, ?) ON CONFLICT DO NOTHING",
            (record_id, version, "known" if supported else "opaque"),
        )

    def record_validated(self, record, *, validator_version: str, supported: bool) -> None:
        """Store a caller-validated fact and its known/opaque status atomically."""
        if type(supported) is not bool:
            raise QuarantineError("semantic support must be explicit")
        _metadata("known" if supported else "unsupported_semantics", validator_version)
        event, canonical, digest, raw_hash = self._prepare(record)
        with self.transaction():
            self._put(record, event, canonical, digest, raw_hash)
            self._mark_validation(record, digest, validator_version, supported)

    def validation_results(self, event_id: str) -> tuple[tuple[int, str, str], ...]:
        return tuple(
            self._db()
            .execute(
                "SELECT v.raw_record_id, v.validator_version, v.semantic_support "
                "FROM event_validations v JOIN raw_records r USING(raw_record_id) "
                "WHERE r.event_id = ? ORDER BY v.raw_record_id, v.validator_version",
                (event_id,),
            )
            .fetchall()
        )

    def quarantine_stream(
        self,
        stream: BinaryIO,
        *,
        location: SourceLocation,
        byte_size: int,
        reason: str,
        validator_version: str,
        collected_at: str,
        event_pointer: str = "",
    ) -> int:
        """Preserve every byte using zeroblob/blobopen; stream must support seek()."""
        _metadata(reason, validator_version)
        self._validate_source(location, byte_size, collected_at)
        if event_pointer not in ("", "/outbox_event") or bool(event_pointer) != (
            location.source_kind == "registry"
        ):
            raise QuarantineError("invalid isolation event pointer")
        if byte_size <= 0 or byte_size > self.max_quarantine_blob_bytes:
            raise QuarantineLimit("isolated record exceeds BLOB capacity")
        stream.seek(0)
        digest, remaining = hashlib.sha256(), byte_size
        while remaining:
            block = _read(stream, min(65536, remaining))
            digest.update(block)
            remaining -= len(block)
        if stream.read(1):
            raise QuarantineError("isolation stream exceeds declared size")
        raw_hash = digest.hexdigest()
        connection = self._db()
        with self.transaction():
            found = connection.execute(
                "SELECT blob_id, byte_size FROM quarantine_blobs WHERE raw_hash = ?", (raw_hash,)
            ).fetchone()
            stream.seek(0)
            if found:
                blob_id, stored_size = found
                if stored_size != byte_size:
                    raise QuarantineError("isolation hash collision")
                with connection.blobopen(
                    "quarantine_blobs", "raw_bytes", blob_id, readonly=True
                ) as blob:
                    remaining = byte_size
                    while remaining:
                        amount = min(65536, remaining)
                        if blob.read(amount) != _read(stream, amount):
                            raise QuarantineError("isolation hash collision")
                        remaining -= amount
            else:
                used = connection.execute(
                    "SELECT coalesce(sum(byte_size), 0) FROM quarantine_blobs"
                ).fetchone()[0]
                if used + byte_size > self.max_quarantine_total_bytes:
                    raise QuarantineLimit("isolation area capacity exhausted")
                cursor = connection.execute(
                    "INSERT INTO quarantine_blobs(raw_hash, byte_size, raw_bytes) VALUES(?, ?, zeroblob(?))",
                    (raw_hash, byte_size, byte_size),
                )
                blob_id = cursor.lastrowid
                with connection.blobopen("quarantine_blobs", "raw_bytes", blob_id) as blob:
                    remaining = byte_size
                    written_hash = hashlib.sha256()
                    while remaining:
                        amount = min(65536, remaining)
                        block = _read(stream, amount)
                        blob.write(block)
                        written_hash.update(block)
                        remaining -= amount
                if written_hash.hexdigest() != raw_hash or stream.read(1):
                    raise QuarantineError("isolation stream changed during persistence")
            location_id = self._location_id(location)
            connection.execute(
                "INSERT INTO quarantine(location_id, blob_id, reason, validator_version, collected_at, "
                "event_pointer) VALUES(?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                (location_id, blob_id, reason, validator_version, collected_at, event_pointer),
            )
            identity = connection.execute(
                "SELECT quarantine_id FROM quarantine WHERE location_id = ? AND blob_id = ? "
                "AND reason = ? AND validator_version = ? AND event_pointer = ?",
                (location_id, blob_id, reason, validator_version, event_pointer),
            ).fetchone()[0]
        return identity

    def _quarantine_rows(self, where: str, parameters: tuple) -> tuple[QuarantinedRecord, ...]:
        from sledra_analysis.ingest.source import SourceKey
        from sledra_analysis.store.raw import SourceLocation

        rows = (
            self._db()
            .execute(
                "SELECT q.quarantine_id, l.source_root_id, l.source_id, l.segment_uuid, l.generation, "
                "l.byte_start, l.byte_end, l.locator, l.source_kind, b.raw_hash, b.byte_size, q.reason, "
                "q.validator_version, q.collected_at, q.event_pointer FROM quarantine q "
                "JOIN source_locations l USING(location_id) JOIN quarantine_blobs b USING(blob_id) "
                + where,
                parameters,
            )
            .fetchall()
        )
        return tuple(
            QuarantinedRecord(row[0], SourceLocation(SourceKey(*row[1:4]), *row[4:9]), *row[9:])
            for row in rows
        )

    def quarantines(self, *, limit: int = 1000, after_id: int = 0) -> tuple[QuarantinedRecord, ...]:
        if (
            type(limit) is not int
            or not 0 < limit <= 10000
            or type(after_id) is not int
            or after_id < 0
        ):
            raise QuarantineError("invalid isolation query page")
        return self._quarantine_rows(
            "WHERE q.quarantine_id > ? ORDER BY q.quarantine_id LIMIT ?", (after_id, limit)
        )

    def quarantine_record(self, quarantine_id: int) -> QuarantinedRecord:
        rows = self._quarantine_rows("WHERE q.quarantine_id = ?", (quarantine_id,))
        if rows:
            return rows[0]
        raise QuarantineError("unknown isolation record")

    def quarantine_chunks(self, quarantine_id: int, *, chunk_size: int = 65536) -> Iterator[bytes]:
        if type(chunk_size) is not int or not 0 < chunk_size <= 1024 * 1024:
            raise QuarantineError("invalid isolation read chunk size")
        record = self.quarantine_record(quarantine_id)
        row = (
            self._db()
            .execute(
                "SELECT blob_id FROM quarantine WHERE quarantine_id = ?", (record.quarantine_id,)
            )
            .fetchone()
        )
        with self._db().blobopen("quarantine_blobs", "raw_bytes", row[0], readonly=True) as blob:
            remaining = record.byte_size
            while remaining:
                block = blob.read(min(remaining, chunk_size))
                if not block:
                    raise QuarantineError("short stored isolation BLOB")
                yield block
                remaining -= len(block)

    def quarantine_bytes(self, quarantine_id: int, *, max_bytes: int | None = None) -> bytes:
        limit = self.max_record_bytes if max_bytes is None else max_bytes
        if type(limit) is not int or limit <= 0:
            raise QuarantineError("invalid isolation materialization budget")
        if self.quarantine_record(quarantine_id).byte_size > limit:
            raise QuarantineLimit("isolated record exceeds materialization budget")
        return b"".join(self.quarantine_chunks(quarantine_id))

    def reprocess(
        self, quarantine_id: int, validator: EventValidator, *, attempted_at: str
    ) -> ReplayResult:
        """Replay the exact original bytes, retain quarantine and append an audit record."""
        from sledra_analysis.store.raw import RawRecord, _timestamp

        if not _timestamp(attempted_at):
            raise QuarantineError("invalid reprocessing time")
        record = self.quarantine_record(quarantine_id)
        _metadata("reprocess", validator.version)
        prepared = None
        if record.byte_size > self.max_record_bytes or record.reason == "sealed_incomplete_tail":
            status, reason = (
                "retained",
                "limit_exceeded" if record.byte_size > self.max_record_bytes else record.reason,
            )
        else:
            raw = self.quarantine_bytes(quarantine_id)
            try:
                result = validator.validate(raw, event_pointer=record.event_pointer)
                item = RawRecord(raw, record.location, record.collected_at, record.event_pointer)
                prepared = self._prepare(item)
                status = "accepted" if result.supported else "opaque"
                reason = "validated" if result.supported else "unsupported_semantics"
            except RecordError as exc:
                status, reason = "retained", exc.code
        _metadata(reason, validator.version)
        with self.transaction():
            event_id = digest = None
            if prepared is not None:
                event, canonical, digest, raw_hash = prepared
                self._put(item, event, canonical, digest, raw_hash)
                self._mark_validation(item, digest, validator.version, result.supported)
                event_id = event["event_id"]
            cursor = self._db().execute(
                "INSERT INTO quarantine_replays(quarantine_id, validator_version, attempted_at, "
                "status, reason, event_id, canonical_hash) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (quarantine_id, validator.version, attempted_at, status, reason, event_id, digest),
            )
            replay = ReplayResult(
                cursor.lastrowid,
                quarantine_id,
                validator.version,
                attempted_at,
                status,
                reason,
                event_id,
                digest,
            )
        return replay

    def replays(self, quarantine_id: int) -> tuple[ReplayResult, ...]:
        return tuple(
            ReplayResult(*row)
            for row in self._db()
            .execute(
                "SELECT replay_id, quarantine_id, validator_version, attempted_at, status, reason, "
                "event_id, canonical_hash FROM quarantine_replays WHERE quarantine_id = ? ORDER BY replay_id",
                (quarantine_id,),
            )
            .fetchall()
        )
