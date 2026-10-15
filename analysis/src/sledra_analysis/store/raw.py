"""Single-writer SQLite raw facts, stable deduplication and retained conflicts.

Transactions cover facts and quarantine records. Durable checkpoints, projection
queues, archival and source acknowledgements belong to later ingestion work.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator
from uuid import UUID

from sledra_analysis.ingest.source import SourceKey
from sledra_analysis.store.canonical import (
    CANONICAL_VERSION,
    RecordError,
    canonical_bytes,
    parse_json,
)
from sledra_analysis.store.ingestion import INGESTION_SCHEMA, IngestionMixin
from sledra_analysis.store.quarantine import QUARANTINE_SCHEMA, QuarantineMixin

SCHEMA_VERSION = 3
APPLICATION_ID = int.from_bytes(b"SLDA", "big")
DEFAULT_MAX_RECORD_BYTES = 1024 * 1024
DEFAULT_MAX_BATCH_RECORDS = 1024
DEFAULT_MAX_BATCH_BYTES = 16 * 1024 * 1024
TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$")

SCHEMA = """
CREATE TABLE store_metadata (
    canonical_version TEXT PRIMARY KEY NOT NULL
) STRICT;
CREATE TABLE events (
    event_id TEXT PRIMARY KEY NOT NULL CHECK(length(event_id) > 0)
) STRICT;
CREATE TABLE event_versions (
    event_id TEXT NOT NULL REFERENCES events(event_id),
    canonical_hash TEXT NOT NULL CHECK(length(canonical_hash) = 64),
    canonical_bytes BLOB NOT NULL,
    event_type TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    PRIMARY KEY(event_id, canonical_hash)
) STRICT;
CREATE TABLE event_conflicts (
    event_id TEXT PRIMARY KEY NOT NULL REFERENCES events(event_id)
) STRICT;
CREATE TRIGGER mark_event_conflict AFTER INSERT ON event_versions
WHEN (SELECT count(*) FROM event_versions WHERE event_id = NEW.event_id) > 1
BEGIN
    INSERT INTO event_conflicts(event_id) VALUES(NEW.event_id)
    ON CONFLICT(event_id) DO NOTHING;
END;
CREATE TABLE raw_blobs (
    raw_hash TEXT PRIMARY KEY NOT NULL CHECK(length(raw_hash) = 64),
    raw_bytes BLOB NOT NULL CHECK(length(raw_bytes) > 0)
) STRICT;
CREATE TABLE source_locations (
    location_id INTEGER PRIMARY KEY,
    source_root_id TEXT NOT NULL CHECK(length(source_root_id) > 0),
    source_id TEXT NOT NULL CHECK(length(source_id) > 0),
    segment_uuid TEXT NOT NULL CHECK(length(segment_uuid) > 0),
    generation INTEGER NOT NULL CHECK(generation >= 0),
    byte_start INTEGER NOT NULL CHECK(byte_start >= 0),
    byte_end INTEGER NOT NULL CHECK(byte_end > byte_start),
    locator TEXT NOT NULL CHECK(length(locator) > 0),
    source_kind TEXT NOT NULL CHECK(source_kind IN ('segment', 'registry')),
    UNIQUE(source_root_id, source_id, segment_uuid, generation,
           byte_start, byte_end, locator, source_kind)
) STRICT;
CREATE TABLE raw_records (
    raw_record_id INTEGER PRIMARY KEY,
    location_id INTEGER NOT NULL REFERENCES source_locations(location_id),
    raw_hash TEXT NOT NULL REFERENCES raw_blobs(raw_hash),
    event_id TEXT NOT NULL,
    canonical_hash TEXT NOT NULL,
    event_pointer TEXT NOT NULL CHECK(event_pointer IN ('', '/outbox_event')),
    collected_at TEXT NOT NULL,
    FOREIGN KEY(event_id, canonical_hash) REFERENCES event_versions(event_id, canonical_hash),
    UNIQUE(location_id, raw_hash, event_pointer)
) STRICT;
CREATE INDEX raw_record_event ON raw_records(event_id, canonical_hash);
"""


class StoreError(RuntimeError):
    """Store is incompatible, unavailable or inconsistent; do not advance ingestion."""


class WriterBusy(StoreError):
    """Another management writer already owns this database."""


@dataclass(frozen=True)
class SourceLocation:
    key: SourceKey
    generation: int
    byte_start: int
    byte_end: int
    locator: str
    source_kind: str = "segment"

    def fields(self) -> tuple:
        return (
            self.key.source_root_id,
            self.key.source_id,
            self.key.segment_uuid,
            self.generation,
            self.byte_start,
            self.byte_end,
            self.locator,
            self.source_kind,
        )


@dataclass(frozen=True)
class RawRecord:
    raw_bytes: bytes
    location: SourceLocation
    collected_at: str
    event_pointer: str = ""


@dataclass(frozen=True)
class StoredSource:
    location: SourceLocation
    raw_hash: str
    raw_bytes: bytes
    collected_at: str
    event_pointer: str


@dataclass(frozen=True)
class EventVersion:
    canonical_hash: str
    canonical_bytes: bytes
    event_type: str
    schema_version: str
    sources: tuple[StoredSource, ...]


@dataclass(frozen=True)
class StoredEvent:
    event_id: str
    conflicted: bool
    versions: tuple[EventVersion, ...]


@dataclass(frozen=True)
class StoreCounts:
    events: int
    versions: int
    raw_records: int
    conflicts: int


@dataclass(frozen=True)
class BatchResult:
    new_events: int
    new_versions: int
    new_sources: int
    conflicting_event_ids: tuple[str, ...]


def _timestamp(value: object) -> bool:
    if not isinstance(value, str) or not TIME.fullmatch(value):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _event(value: object, pointer: str) -> dict:
    if pointer not in ("", "/outbox_event"):
        raise RecordError("unsupported event pointer")
    if pointer:
        if not isinstance(value, dict) or "outbox_event" not in value:
            raise RecordError("missing Registry outbox event")
        value = value["outbox_event"]
    if not isinstance(value, dict) or value.get("kind") != "event":
        raise RecordError("expected an event envelope")
    if not isinstance(value.get("schema_version"), str) or not re.fullmatch(
        r"1\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", value["schema_version"]
    ):
        code = (
            "unsupported_major"
            if isinstance(value.get("schema_version"), str)
            and not value["schema_version"].startswith("1.")
            else "schema_invalid"
        )
        raise RecordError("unsupported event schema version", code)
    identity = value.get("event_id")
    if not isinstance(identity, str):
        raise RecordError("missing event_id")
    if not re.fullmatch(r"evt_[0-9a-f]{64}", identity):
        try:
            if UUID(identity).version != 4 or str(UUID(identity)) != identity:
                raise ValueError
        except ValueError as exc:
            raise RecordError("invalid event_id") from exc
    if not isinstance(value.get("event_type"), str) or not re.fullmatch(
        r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+", value["event_type"]
    ):
        raise RecordError("missing or invalid event_type")
    for key in ("producer", "source", "time_quality", "association", "payload"):
        if not isinstance(value.get(key), dict):
            raise RecordError(f"missing event object: {key}")
    if not _timestamp(value.get("recorded_at")):
        raise RecordError("recorded_at must include a valid timezone")
    if "occurred_at" not in value or (
        value["occurred_at"] is not None and not _timestamp(value["occurred_at"])
    ):
        raise RecordError("invalid occurred_at")
    # This is a storage boundary, not payload/role/parent-reference validation.
    # Preserve every field, including future extensions and unknown event types.
    return value


class RawStore(IngestionMixin, QuarantineMixin):
    """Own one disk database and its advisory writer lock until close()."""

    def __init__(
        self,
        path: Path,
        *,
        max_record_bytes: int = DEFAULT_MAX_RECORD_BYTES,
        max_batch_records: int = DEFAULT_MAX_BATCH_RECORDS,
        max_batch_bytes: int = DEFAULT_MAX_BATCH_BYTES,
        max_quarantine_blob_bytes: int = 64 * 1024 * 1024,
        max_quarantine_total_bytes: int = 512 * 1024 * 1024,
        upgrade: bool = False,
    ):
        if any(
            type(limit) is not int or limit <= 0
            for limit in (
                max_record_bytes,
                max_batch_records,
                max_batch_bytes,
                max_quarantine_blob_bytes,
                max_quarantine_total_bytes,
            )
        ):
            raise ValueError("store budgets must be positive integers")
        if type(upgrade) is not bool:
            raise ValueError("upgrade flag must be boolean")
        if sqlite3.sqlite_version_info < (3, 37, 0):
            raise StoreError("SQLite 3.37+ is required for STRICT tables")
        self.path = Path(path).absolute()
        self.max_record_bytes = max_record_bytes
        self.max_batch_records = max_batch_records
        self.max_batch_bytes = max_batch_bytes
        self.max_quarantine_blob_bytes = max_quarantine_blob_bytes
        self.max_quarantine_total_bytes = max_quarantine_total_bytes
        self._lock_fd = None
        self._connection = None
        self._active = False
        self._joining_ingest = False
        self._owner_pid = os.getpid()
        try:
            # Lock the database inode, not a pathname sidecar: hard-link aliases
            # must not bypass the lifetime single-writer gate.
            self._lock_fd = os.open(
                self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600
            )
            if not stat.S_ISREG(os.fstat(self._lock_fd).st_mode):
                raise StoreError("database must be a regular file")
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WriterBusy("database already has a writer") from exc
            self._connection = sqlite3.connect(str(self.path), timeout=0, isolation_level=None)
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._initialize(upgrade=upgrade)
        except BaseException:
            self.close()
            raise

    def _initialize(self, *, upgrade: bool) -> None:
        connection = self._db()
        identity = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        fresh = identity == 0 and version == 0 and not tables
        migration = identity == APPLICATION_ID and version in (1, 2)
        if migration and not upgrade:
            raise StoreError(f"schema {version} requires explicit upgrade=True")
        if (
            not fresh
            and not migration
            and (identity != APPLICATION_ID or version != SCHEMA_VERSION)
        ):
            raise StoreError("unrecognized or unsupported database schema")
        if fresh:
            # executescript owns this explicit DDL transaction. A failed creation
            # leaves no partial schema to mistake for a usable store on restart.
            try:
                connection.executescript(
                    "BEGIN IMMEDIATE;\n" + SCHEMA + QUARANTINE_SCHEMA + INGESTION_SCHEMA
                )
                connection.execute("INSERT INTO store_metadata VALUES(?)", (CANONICAL_VERSION,))
                connection.execute(f"PRAGMA application_id = {APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        if connection.execute("SELECT canonical_version FROM store_metadata").fetchall() != [
            (CANONICAL_VERSION,)
        ]:
            raise StoreError("unsupported canonical encoding version")
        if migration:
            try:
                connection.executescript(
                    "BEGIN IMMEDIATE;\n"
                    + (QUARANTINE_SCHEMA if version == 1 else "")
                    + INGESTION_SCHEMA
                )
                connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        # Explicit management-side DELETE journal: never infer WAL deployment
        # suitability from a shared spool location.
        connection.execute("PRAGMA journal_mode = DELETE")
        connection.execute("PRAGMA synchronous = FULL")

    def _db(self) -> sqlite3.Connection:
        if os.getpid() != self._owner_pid:
            raise StoreError("store connection cannot be used after fork")
        if self._connection is None:
            raise StoreError("store is closed")
        return self._connection

    def close(self) -> None:
        try:
            if self._connection is not None:
                self._connection.close()
        finally:
            self._connection = None
            self._active = False
            if self._lock_fd is not None:
                os.close(self._lock_fd)
                self._lock_fd = None

    def __enter__(self) -> RawStore:
        self._db()
        return self

    def __exit__(self, *args) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[RawStore]:
        connection = self._db()
        if self._active:
            raise StoreError("nested write transactions are not supported")
        connection.execute("BEGIN IMMEDIATE")
        self._active = True
        try:
            yield self
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            self._active = False

    def _prepare(self, record: RawRecord) -> tuple[dict, bytes, str, str]:
        raw, location = record.raw_bytes, record.location
        if not isinstance(raw, bytes) or not 0 < len(raw) <= self.max_record_bytes:
            raise RecordError("raw record exceeds byte budget or is empty", "limit_exceeded")
        self._validate_source(location, len(raw), record.collected_at)
        if bool(record.event_pointer) != (location.source_kind == "registry"):
            raise RecordError("Registry records require an outbox event pointer")
        value = _event(parse_json(raw), record.event_pointer)
        canonical = canonical_bytes(value)
        if len(canonical) > self.max_record_bytes:
            raise RecordError("canonical event exceeds byte budget", "limit_exceeded")
        return (
            value,
            canonical,
            hashlib.sha256(canonical).hexdigest(),
            hashlib.sha256(raw).hexdigest(),
        )

    def _validate_source(self, location: SourceLocation, size: int, collected_at: str) -> None:
        for value in (location.generation, location.byte_start, location.byte_end):
            if type(value) is not int or not 0 <= value < (1 << 63):
                raise RecordError("source position must be a nonnegative signed 64-bit integer")
        if location.byte_end - location.byte_start != size:
            raise RecordError("source range does not match raw byte length")
        if any(
            not isinstance(value, str) or not value
            for value in (
                location.key.source_root_id,
                location.key.source_id,
                location.key.segment_uuid,
                location.locator,
            )
        ):
            raise RecordError("source identity and locator must be explicit")
        if location.source_kind not in ("segment", "registry"):
            raise RecordError("unsupported source kind")
        if not _timestamp(collected_at):
            raise RecordError("collection time must include a valid timezone")

    def ingest(self, records: Iterable[RawRecord]) -> BatchResult:
        """Atomically store a bounded batch. Validation failure writes nothing."""
        prepared = []
        total = 0
        for record in records:
            if len(prepared) >= self.max_batch_records:
                raise RecordError("batch exceeds record budget")
            if not isinstance(record.raw_bytes, bytes):
                raise RecordError("raw input must be bytes")
            total += len(record.raw_bytes)
            if total > self.max_batch_bytes:
                raise RecordError("batch exceeds byte budget")
            prepared.append((record, self._prepare(record)))
        new_events = new_versions = new_sources = 0
        identities = set()
        with self._write_scope():
            for record, (event, canonical, digest, raw_hash) in prepared:
                added = self._put(record, event, canonical, digest, raw_hash)
                new_events += added[0]
                new_versions += added[1]
                new_sources += added[2]
                identities.add(event["event_id"])
            conflicts = tuple(
                sorted(identity for identity in identities if self._conflicted(identity))
            )
        return BatchResult(new_events, new_versions, new_sources, conflicts)

    def _put(
        self, record: RawRecord, event: dict, canonical: bytes, digest: str, raw_hash: str
    ) -> tuple[int, int, int]:
        connection = self._db()
        identity = event["event_id"]
        added_event = connection.execute(
            "INSERT INTO events VALUES(?) ON CONFLICT(event_id) DO NOTHING", (identity,)
        ).rowcount
        added_version = connection.execute(
            "INSERT INTO event_versions VALUES(?, ?, ?, ?, ?) "
            "ON CONFLICT(event_id, canonical_hash) DO NOTHING",
            (identity, digest, canonical, event["event_type"], event["schema_version"]),
        ).rowcount
        if (
            connection.execute(
                "SELECT canonical_bytes FROM event_versions WHERE event_id = ? AND canonical_hash = ?",
                (identity, digest),
            ).fetchone()[0]
            != canonical
        ):
            raise StoreError("canonical hash collision; retain input and stop batch")
        connection.execute(
            "INSERT INTO raw_blobs VALUES(?, ?) ON CONFLICT(raw_hash) DO NOTHING",
            (raw_hash, record.raw_bytes),
        )
        if (
            connection.execute(
                "SELECT raw_bytes FROM raw_blobs WHERE raw_hash = ?", (raw_hash,)
            ).fetchone()[0]
            != record.raw_bytes
        ):
            raise StoreError("raw hash collision; retain input and stop batch")
        location_id = self._location_id(record.location)
        added_source = connection.execute(
            "INSERT INTO raw_records(location_id, raw_hash, event_id, canonical_hash, "
            "event_pointer, collected_at) VALUES(?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(location_id, raw_hash, event_pointer) DO NOTHING",
            (location_id, raw_hash, identity, digest, record.event_pointer, record.collected_at),
        ).rowcount
        return added_event, added_version, added_source

    def _location_id(self, location: SourceLocation) -> int:
        connection = self._db()
        connection.execute(
            "INSERT INTO source_locations(source_root_id, source_id, segment_uuid, generation, "
            "byte_start, byte_end, locator, source_kind) VALUES(?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT DO NOTHING",
            location.fields(),
        )
        location_id = connection.execute(
            "SELECT location_id FROM source_locations WHERE source_root_id = ? AND source_id = ? "
            "AND segment_uuid = ? AND generation = ? AND byte_start = ? AND byte_end = ? "
            "AND locator = ? AND source_kind = ?",
            location.fields(),
        ).fetchone()[0]
        return location_id

    def _conflicted(self, identity: str) -> bool:
        return (
            self._db()
            .execute("SELECT 1 FROM event_conflicts WHERE event_id = ?", (identity,))
            .fetchone()
            is not None
        )

    def counts(self) -> StoreCounts:
        connection = self._db()
        counts = [
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("events", "event_versions", "raw_records", "event_conflicts")
        ]
        return StoreCounts(*counts)

    def get_event(self, identity: str) -> StoredEvent | None:
        connection = self._db()
        if (
            connection.execute("SELECT 1 FROM events WHERE event_id = ?", (identity,)).fetchone()
            is None
        ):
            return None
        versions = tuple(
            EventVersion(digest, raw, kind, version, self.sources(identity, digest))
            for digest, raw, kind, version in connection.execute(
                "SELECT canonical_hash, canonical_bytes, event_type, schema_version "
                "FROM event_versions WHERE event_id = ? ORDER BY canonical_hash",
                (identity,),
            ).fetchall()
        )
        return StoredEvent(identity, self._conflicted(identity), versions)

    def events(self, *, conflicts_only: bool = False) -> tuple[StoredEvent, ...]:
        table = "event_conflicts" if conflicts_only else "events"
        identities = (
            self._db().execute(f"SELECT event_id FROM {table} ORDER BY event_id").fetchall()
        )
        return tuple(self.get_event(identity) for (identity,) in identities)

    def sources(self, identity: str, canonical_hash: str | None = None) -> tuple[StoredSource, ...]:
        rows = (
            self._db()
            .execute(
                "SELECT l.source_root_id, l.source_id, l.segment_uuid, l.generation, l.byte_start, "
                "l.byte_end, l.locator, l.source_kind, r.raw_hash, b.raw_bytes, r.collected_at, "
                "r.event_pointer FROM raw_records r JOIN source_locations l USING(location_id) "
                "JOIN raw_blobs b USING(raw_hash) WHERE r.event_id = ? "
                "AND (? IS NULL OR r.canonical_hash = ?) "
                "ORDER BY l.source_root_id, l.source_id, l.segment_uuid, l.generation, l.byte_start, "
                "l.byte_end, l.locator, l.source_kind, r.raw_hash, r.event_pointer",
                (identity, canonical_hash, canonical_hash),
            )
            .fetchall()
        )
        return tuple(
            StoredSource(SourceLocation(SourceKey(*row[:3]), *row[3:8]), *row[8:]) for row in rows
        )
