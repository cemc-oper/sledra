"""Bounded JSONL scanning with durable facts/isolation before proposing a position."""

from __future__ import annotations

import io
import tempfile
from dataclasses import dataclass
from typing import BinaryIO

from sledra_analysis.ingest.source import Checkpoint, SourceLease, SourceStream
from sledra_analysis.ingest.validation import EventValidator
from sledra_analysis.store.canonical import RecordError
from sledra_analysis.store.quarantine import QuarantineLimit
from sledra_analysis.store.raw import RawRecord, RawStore, SourceLocation


@dataclass(frozen=True)
class ScanResult:
    checkpoint: Checkpoint
    records: int
    quarantine_ids: tuple[int, ...]
    opaque_event_ids: tuple[str, ...]
    diagnostics: tuple[str, ...]
    pending_tail_bytes: int
    scanned_bytes: int


class _Frame:
    def __init__(self, start: int, memory_limit: int, storage_limit: int, directory):
        self.start, self.size = start, 0
        self._memory_limit, self._storage_limit, self._directory = (
            memory_limit,
            storage_limit,
            directory,
        )
        self._memory = bytearray()
        self._file = None

    def append(self, block: bytes) -> None:
        if self.size + len(block) > self._storage_limit:
            raise QuarantineLimit("line cannot fit isolation capacity; retain source position")
        if self._file is None and self.size + len(block) > self._memory_limit:
            self._file = tempfile.TemporaryFile(dir=self._directory)
            self._file.write(self._memory)
            self._memory.clear()
        if self._file is None:
            self._memory.extend(block)
        else:
            self._file.write(block)
        self.size += len(block)

    @property
    def oversized(self) -> bool:
        return self.size > self._memory_limit

    def bytes(self) -> bytes:
        if self.oversized:
            raise QuarantineLimit("oversized line must be streamed")
        return bytes(self._memory)

    def stream(self) -> BinaryIO:
        if self._file is None:
            return io.BytesIO(self._memory)
        self._file.seek(0)
        return self._file

    def close(self) -> None:
        if self._file is not None:
            self._file.close()


class BoundedReader:
    """Single-reader, bounded scan; storage failure raises without returning a checkpoint."""

    def __init__(
        self,
        store: RawStore,
        *,
        validator: EventValidator | None = None,
        max_line_bytes: int = 1024 * 1024,
        chunk_size: int = 65536,
        max_records: int = 1024,
        max_scan_bytes: int = 64 * 1024 * 1024,
    ):
        if (
            any(
                type(value) is not int or value <= 0
                for value in (max_line_bytes, chunk_size, max_records, max_scan_bytes)
            )
            or chunk_size > 1024 * 1024
        ):
            raise ValueError("reader budgets must be positive; chunk size is at most 1 MiB")
        self.store, self.validator = store, validator or EventValidator()
        self.max_line_bytes, self.chunk_size = max_line_bytes, chunk_size
        self.max_records, self.max_scan_bytes = max_records, max_scan_bytes

    def _frame(self, start: int) -> _Frame:
        return _Frame(
            start, self.max_line_bytes, self.store.max_quarantine_blob_bytes, self.store.path.parent
        )

    def _persist(
        self,
        frame: _Frame,
        lease: SourceLease,
        stream: SourceStream,
        locator: str,
        collected_at: str,
        *,
        sealed_tail: bool = False,
    ) -> tuple[int | None, str | None]:
        state = lease.decision.checkpoint
        location = SourceLocation(
            state.key, state.generation, frame.start, frame.start + frame.size, locator
        )
        reason = (
            "sealed_incomplete_tail"
            if sealed_tail
            else "limit_exceeded"
            if frame.oversized
            else None
        )
        stream.verify()
        if reason is None:
            raw = frame.bytes()
            try:
                validated = self.validator.validate(raw)
                self.store.record_validated(
                    RawRecord(raw, location, collected_at),
                    validator_version=self.validator.version,
                    supported=validated.supported,
                )
            except RecordError as exc:
                reason = exc.code
            else:
                stream.verify()
                return None, None if validated.supported else validated.event["event_id"]
        preserved = frame.stream()
        try:
            identity = self.store.quarantine_stream(
                preserved,
                location=location,
                byte_size=frame.size,
                reason=reason,
                validator_version=self.validator.version,
                collected_at=collected_at,
            )
        finally:
            if preserved is not frame._file:
                preserved.close()
        stream.verify()
        return identity, None

    def process(self, lease: SourceLease, *, locator: str, collected_at: str) -> ScanResult:
        state = lease.decision.checkpoint
        if lease.decision.resume_offset is None:
            raise ValueError("conflicted segment cannot be scanned")
        start = state.committed_offset
        if state.protocol_version is not None:
            start = max(start, state.header_size)
        safe_offset = start if start == state.last_size else state.committed_offset
        frame = self._frame(start)
        quarantined, opaque = [], []
        diagnostics = list(lease.decision.diagnostics)
        records = scanned = pending = 0
        sealed_residual = stopped = False
        try:
            with lease.stream(start, chunk_size=self.chunk_size) as stream:
                for chunk in stream.chunks():
                    cursor = 0
                    while cursor < len(chunk):
                        remaining = self.max_scan_bytes - scanned
                        if remaining == 0:
                            diagnostics.append("scan_byte_budget_reached")
                            stopped = True
                            break
                        end = chunk.find(b"\n", cursor)
                        line_complete = end >= 0 and end + 1 - cursor <= remaining
                        stop = end + 1 if line_complete else min(len(chunk), cursor + remaining)
                        piece = chunk[cursor:stop]
                        frame.append(piece)
                        scanned += len(piece)
                        cursor = stop
                        if line_complete:
                            identity, unknown = self._persist(
                                frame, lease, stream, locator, collected_at
                            )
                            if identity is not None:
                                quarantined.append(identity)
                            if unknown is not None:
                                opaque.append(unknown)
                                diagnostics.append("unsupported_semantics")
                            safe_offset = frame.start + frame.size
                            records += 1
                            frame.close()
                            frame = self._frame(safe_offset)
                            if records >= self.max_records and safe_offset < state.last_size:
                                diagnostics.append("scan_record_budget_reached")
                                stopped = True
                                break
                    if stopped:
                        break
                if frame.size:
                    if not stopped and state.sealed_hash is not None:
                        identity, _ = self._persist(
                            frame, lease, stream, locator, collected_at, sealed_tail=True
                        )
                        quarantined.append(identity)
                        safe_offset = frame.start + frame.size
                        records += 1
                        sealed_residual = True
                    else:
                        pending = frame.size
                        diagnostics.append(
                            "scan_partial_record"
                            if stopped
                            else "stuck_tail"
                            if frame.oversized
                            else "open_partial_tail"
                        )
            # A proposal only, after successful durable writes and final source verification.
            already_sealed_eof = (
                state.sealed_hash is not None
                and safe_offset == state.committed_offset == state.sealed_size
            )
            proposed = lease.checkpoint(
                safe_offset, sealed_tail=sealed_residual or already_sealed_eof
            )
            return ScanResult(
                proposed,
                records,
                tuple(quarantined),
                tuple(opaque),
                tuple(dict.fromkeys(diagnostics)),
                pending,
                scanned,
            )
        finally:
            frame.close()
