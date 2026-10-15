"""Management-side atomic ingestion. No execution or business recovery actions."""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from sledra_analysis.ingest.reader import BoundedReader, ScanResult
from sledra_analysis.ingest.source import (
    SourceChanged,
    SourceError,
    SourceKey,
    SourceTracker,
    _open,
    _stamp,
)
from sledra_analysis.store.canonical import RecordError
from sledra_analysis.store.raw import RawRecord, RawStore, SourceLocation


@dataclass(frozen=True)
class RegistryResult:
    raw_hash: str
    duplicate: bool
    quarantine_id: int | None
    commit_id: int
    diagnostics: tuple[str, ...]


class AtomicIngestor:
    """Recreate the tracker from committed state for every bounded transaction."""

    def __init__(self, store: RawStore, root: Path, source_root_id: str, **reader_options):
        self.store, self.root, self.source_root_id = store, Path(root), source_root_id
        self.reader = BoundedReader(store, **reader_options)

    def _stage(self, name: str) -> None:
        """Internal fault-injection seam; production performs no extra action."""

    def scan(
        self, locator: str, *, collected_at: str, legacy_source_id: str | None = None
    ) -> ScanResult:
        tracker = SourceTracker(
            self.root, self.source_root_id, self.store.checkpoints(self.source_root_id)
        )
        conflicted = False
        with tracker.open(locator, legacy_source_id=legacy_source_id) as lease:
            with self.store._ingestion_transaction(collected_at) as commit:
                if lease.decision.resume_offset is None:
                    conflicted = True
                    for state in tracker.checkpoints:
                        if state.key == lease.decision.checkpoint.key:
                            self.store._save_checkpoint(state, lease.decision.diagnostics, commit)
                else:
                    result = self.reader.process(lease, locator=locator, collected_at=collected_at)
                    self._stage("after_records")
                    self.store._save_checkpoint(result.checkpoint, result.diagnostics, commit)
                self._stage("after_checkpoint")
                lease._verify()
                self._stage("before_commit")
            self._stage("after_commit")
        if conflicted:
            raise SourceError("segment UUID conflict persisted; normal advancement blocked")
        return result

    def registry(self, locator: str, *, source_id: str, collected_at: str) -> RegistryResult:
        """Persist a whole immutable Registry JSON (or isolation) and digest together."""
        if not source_id:
            raise SourceError("Registry source namespace is required")
        with _open(self.root, locator) as fd:
            stamp = _stamp(os.fstat(fd))
            size = stamp[2]
            if not 0 < size <= self.store.max_quarantine_blob_bytes:
                raise SourceError("Registry file cannot fit preservation budget")
            digest = hashlib.sha256()
            offset = 0
            while offset < size:
                block = os.pread(fd, min(65536, size - offset), offset)
                if not block:
                    raise SourceChanged("short Registry read")
                digest.update(block)
                offset += len(block)
            raw_hash = digest.hexdigest()

            def verify():
                if _stamp(os.fstat(fd)) != stamp:
                    raise SourceChanged("Registry changed during ingestion")

            verify()
            with self.store._ingestion_transaction(collected_at) as commit:
                previous = (
                    self.store._db()
                    .execute(
                        "SELECT raw_hash FROM registry_checkpoints WHERE source_root_id=? AND source_id=? AND locator=?",
                        (self.source_root_id, source_id, locator),
                    )
                    .fetchall()
                )
                duplicate = (raw_hash,) in previous
                quarantine_id = None
                diagnostics = ("registry_immutable_conflict",) if previous and not duplicate else ()
                if not duplicate:
                    key = SourceKey(
                        self.source_root_id,
                        source_id,
                        "registry:" + hashlib.sha256(locator.encode()).hexdigest(),
                    )
                    location = SourceLocation(key, len(previous), 0, size, locator, "registry")
                    reason = "limit_exceeded" if size > self.store.max_record_bytes else None
                    if reason is None:
                        raw = os.pread(fd, size, 0)
                        if len(raw) != size or hashlib.sha256(raw).hexdigest() != raw_hash:
                            raise SourceChanged("Registry changed between reads")
                        try:
                            validated = self.reader.validator.validate(
                                raw, event_pointer="/outbox_event"
                            )
                            self.store.record_validated(
                                RawRecord(raw, location, collected_at, "/outbox_event"),
                                validator_version=self.reader.validator.version,
                                supported=validated.supported,
                            )
                        except RecordError as exc:
                            reason = exc.code
                    if reason:
                        with os.fdopen(os.dup(fd), "rb") as stream:
                            quarantine_id = self.store.quarantine_stream(
                                stream,
                                location=location,
                                byte_size=size,
                                reason=reason,
                                validator_version=self.reader.validator.version,
                                collected_at=collected_at,
                                event_pointer="/outbox_event",
                            )
                    self._stage("after_records")
                    self.store._db().execute(
                        "INSERT INTO registry_checkpoints VALUES(?, ?, ?, ?, ?, ?)",
                        (self.source_root_id, source_id, locator, raw_hash, size, commit),
                    )
                self._stage("after_checkpoint")
                verify()
                self._stage("before_commit")
            self._stage("after_commit")
        return RegistryResult(raw_hash, duplicate, quarantine_id, commit, diagnostics)
