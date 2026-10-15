"""Identify source generations without treating a pathname as a segment identity.

The tracker is single-reader, in-memory state. AtomicIngestor persists Checkpoint
alongside facts; nothing here acknowledges or deletes sources.
Legacy sources require an explicit namespace and always carry a limited-integrity warning.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterator
from uuid import UUID

WINDOW = 4096
MAX_HEADER = 65536


class SourceError(ValueError):
    """Source cannot be safely inspected; retain the previous position and retry."""


class SourceChanged(SourceError):
    """The inspected file changed during a scan or read."""


@dataclass(frozen=True)
class SourceKey:
    source_root_id: str
    source_id: str
    segment_uuid: str


@dataclass(frozen=True)
class Checkpoint:
    key: SourceKey
    generation: int
    protocol_version: str | None
    discovered_paths: tuple[str, ...]
    device: int
    inode: int
    header_size: int
    header_hash: str
    committed_offset: int
    anchor_hash: str
    last_size: int
    history_hash: str
    sealed_size: int | None
    sealed_hash: str | None
    scan_epoch: int
    conflict: bool = False


@dataclass(frozen=True)
class Decision:
    checkpoint: Checkpoint
    action: str
    diagnostics: tuple[str, ...]
    resume_offset: int | None


def _bytes(fd: int, start: int, length: int) -> bytes:
    chunks = []
    while length:
        block = os.pread(fd, length, start)
        if not block:
            raise SourceChanged("short read; retry without advancing offset")
        chunks.append(block)
        start += len(block)
        length -= len(block)
    return b"".join(chunks)


def _hash(fd: int, start: int, length: int) -> str:
    digest = hashlib.sha256()
    while length:
        amount = min(length, 65536)
        digest.update(_bytes(fd, start, amount))
        start += amount
        length -= amount
    return digest.hexdigest()


def _anchor(fd: int, offset: int) -> str:
    start = max(0, offset - WINDOW)
    return _hash(fd, start, offset - start)


def _unique(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise SourceError("duplicate segment header key")
        result[key] = value
    return result


def _header(fd: int) -> tuple[dict, int]:
    # A header is bounded independently of the future event-line reader.
    raw = os.pread(fd, MAX_HEADER + 1, 0).split(b"\n", 1)[0]
    if len(raw) > MAX_HEADER or os.pread(fd, 1, len(raw)) != b"\n":
        raise SourceError("incomplete or oversized segment header")
    try:
        value = json.loads(raw, object_pairs_hook=_unique)
        if not isinstance(value, dict) or value.get("record_kind") != "segment_header":
            raise SourceError("missing segment header; legacy input must be explicit")
        for name in ("source_id", "segment_uuid", "writer_session", "created_at"):
            if not isinstance(value.get(name), str) or not value[name]:
                raise SourceError(f"missing segment header field: {name}")
        if value.get("protocol_version") != "1.0.0":
            raise SourceError("unsupported segment protocol version")
        value["segment_uuid"] = str(UUID(value["segment_uuid"]))
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        if isinstance(exc, SourceError):
            raise
        raise SourceError("invalid segment header") from exc
    return value, len(raw) + 1


@contextmanager
def _open(root: Path, locator: str) -> Iterator[int]:
    path = Path(locator)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise SourceError("locator must be relative to the configured root")
    # Each directory component is opened without following symlinks. Root is trusted
    # deployment configuration, but its final component must also be a real directory.
    directories = []
    fd = None
    try:
        directories.append(os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW))
        for part in path.parts[:-1]:
            directories.append(
                os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directories[-1])
            )
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directories[-1])
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SourceError("source must be a regular file")
        yield fd
    finally:
        if fd is not None:
            os.close(fd)
        for directory in reversed(directories):
            os.close(directory)


def _stamp(info: os.stat_result) -> tuple[int, ...]:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _matches(fd: int, state: Checkpoint, size: int) -> bool:
    return (
        size >= max(state.last_size, state.committed_offset, state.header_size)
        and _hash(fd, 0, state.header_size) == state.header_hash
        and _anchor(fd, state.committed_offset) == state.anchor_hash
        and _anchor(fd, state.last_size) == state.history_hash
        and (
            state.sealed_hash is None
            or (size == state.sealed_size and _hash(fd, 0, size) == state.sealed_hash)
        )
    )


class SourceStream:
    """Bounded pread stream with one pinned metadata stamp for the scan."""

    def __init__(self, fd: int, offset: int, size: int, chunk_size: int):
        self._fd, self._offset, self._size, self._chunk_size = fd, offset, size, chunk_size
        self._stamp = _stamp(os.fstat(fd))

    def verify(self) -> None:
        if _stamp(os.fstat(self._fd)) != self._stamp:
            raise SourceChanged("source changed during stream; discard position and rescan")

    def chunks(self) -> Iterator[bytes]:
        offset = self._offset
        while offset < self._size:
            self.verify()
            block = _bytes(self._fd, offset, min(self._chunk_size, self._size - offset))
            self.verify()
            yield block
            offset += len(block)
        self.verify()


class SourceLease:
    """Read through the inspected descriptor, including across an open→sealed rename."""

    def __init__(self, fd: int, decision: Decision):
        self._fd = fd
        self.decision = decision

    def _verify(self) -> None:
        state = self.decision.checkpoint
        info = os.fstat(self._fd)
        if (info.st_dev, info.st_ino) != (state.device, state.inode):
            raise SourceChanged("descriptor identity changed")
        if not _matches(self._fd, state, info.st_size):
            raise SourceChanged("source changed; discard read and rescan")

    def read(self, offset: int, length: int) -> bytes:
        if self.decision.resume_offset is None:
            raise SourceError("segment conflict; normal reading is blocked")
        if offset < self.decision.resume_offset or length < 0:
            raise SourceError("invalid read range")
        before = _stamp(os.fstat(self._fd))
        self._verify()
        # Read only the captured size; later appends belong to the next scan.
        size = self.decision.checkpoint.last_size
        result = _bytes(self._fd, min(offset, size), min(length, max(0, size - offset)))
        self._verify()
        if before != _stamp(os.fstat(self._fd)):
            raise SourceChanged("source changed during read; discard bytes and retry")
        return result

    @contextmanager
    def archive_stream(self) -> Iterator[SourceStream]:
        """Read the entire verified immutable segment, including its header."""
        if self.decision.resume_offset is None or self.decision.checkpoint.sealed_hash is None:
            raise SourceError("archive requires a verified immutable segment")
        self._verify()
        stream = SourceStream(self._fd, 0, self.decision.checkpoint.last_size, 65536)
        yield stream
        stream.verify()
        self._verify()
        stream.verify()

    @contextmanager
    def stream(self, offset: int, *, chunk_size: int = 65536) -> Iterator[SourceStream]:
        if self.decision.resume_offset is None or offset < self.decision.resume_offset:
            raise SourceError("invalid or conflicted stream position")
        if type(chunk_size) is not int or not 0 < chunk_size <= 1024 * 1024:
            raise SourceError("stream chunk size must be between 1 and 1048576")
        self._verify()
        stream = SourceStream(self._fd, offset, self.decision.checkpoint.last_size, chunk_size)
        yield stream
        stream.verify()
        self._verify()
        stream.verify()

    def checkpoint(self, offset: int, *, sealed_tail: bool = False) -> Checkpoint:
        """Propose a line boundary or explicitly persisted immutable sealed residual."""
        state = self.decision.checkpoint
        if (
            self.decision.resume_offset is None
            or not state.committed_offset <= offset <= state.last_size
        ):
            raise SourceError("invalid checkpoint offset")
        before = _stamp(os.fstat(self._fd))
        self._verify()
        tail_allowed = sealed_tail and state.sealed_hash is not None and offset == state.sealed_size
        if sealed_tail and not tail_allowed:
            raise SourceError("sealed residual position must match the immutable sealed EOF")
        if offset and os.pread(self._fd, 1, offset - 1) != b"\n" and not tail_allowed:
            raise SourceError("checkpoint must end at a complete line")
        updated = replace(state, committed_offset=offset, anchor_hash=_anchor(self._fd, offset))
        self._verify()
        if before != _stamp(os.fstat(self._fd)):
            raise SourceChanged("source changed while proposing checkpoint")
        return updated


class SourceTracker:
    """A single-reader identity catalogue, restorable from caller-owned checkpoints."""

    def __init__(self, root: Path, source_root_id: str, checkpoints: tuple[Checkpoint, ...] = ()):
        if not source_root_id:
            raise SourceError("source_root_id must be explicit")
        self.root = Path(root)
        self.source_root_id = source_root_id
        self._states: dict[tuple[SourceKey, int], Checkpoint] = {}
        self._epoch = 0
        for state in checkpoints:
            self.remember(state)

    @property
    def checkpoints(self) -> tuple[Checkpoint, ...]:
        return tuple(self._states.values())

    def remember(self, state: Checkpoint) -> None:
        """Install caller-persisted state; this method performs no durable commit."""
        if state.key.source_root_id != self.source_root_id:
            raise SourceError("checkpoint belongs to another source root")
        self._states[state.key, state.generation] = state
        self._epoch = max(self._epoch, state.scan_epoch)

    @contextmanager
    def open(self, locator: str, *, legacy_source_id: str | None = None) -> Iterator[SourceLease]:
        locator = str(Path(locator))
        with _open(self.root, locator) as fd:
            before = os.fstat(fd)
            decision = self._inspect(fd, locator, before, legacy_source_id)
            if _stamp(before) != _stamp(os.fstat(fd)):
                raise SourceChanged("source changed during inspection; retry")
            # Install only after a stable scan. No facts or offsets are committed here.
            if decision.action == "conflict":
                for identity, state in tuple(self._states.items()):
                    if state.key == decision.checkpoint.key:
                        self._states[identity] = replace(state, conflict=True)
            self.remember(decision.checkpoint)
            yield SourceLease(fd, decision)

    def _inspect(
        self, fd: int, locator: str, info: os.stat_result, legacy_source_id: str | None
    ) -> Decision:
        self._epoch += 1
        legacy = legacy_source_id is not None
        if legacy:
            if not legacy_source_id:
                raise SourceError("legacy source namespace must not be empty")
            # Namespace + discovery locator bootstraps identity, then inode/fingerprints
            # identify rename. No inode is used as a durable global identifier.
            token = hashlib.sha256(locator.encode()).hexdigest()
            key = SourceKey(self.source_root_id, legacy_source_id, f"legacy:{token}")
            version, header_size = None, min(WINDOW, info.st_size)
        else:
            header, header_size = _header(fd)
            key = SourceKey(self.source_root_id, header["source_id"], header["segment_uuid"])
            version = header["protocol_version"]
        candidates = [state for state in self._states.values() if state.key == key]
        if legacy:
            located = [
                state
                for state in self._states.values()
                if state.protocol_version is None
                and state.key.source_id == legacy_source_id
                and locator in state.discovered_paths
            ]
            if located:
                key = max(located, key=lambda state: state.scan_epoch).key
                candidates = [state for state in self._states.values() if state.key == key]
        if legacy and not candidates:
            candidates = [
                state
                for state in self._states.values()
                if state.protocol_version is None
                and state.key.source_id == legacy_source_id
                and (state.device, state.inode) == (info.st_dev, info.st_ino)
                and _matches(fd, state, info.st_size)
            ]
            if candidates:
                key = candidates[-1].key
        previous = max(candidates, key=lambda state: state.generation, default=None)
        sealed = not legacy and locator.endswith(".sealed.jsonl")
        diagnostics = ["legacy_source_integrity_unproven"] if legacy else []
        action, offset = "new", 0
        generation = previous.generation + 1 if previous else 0
        paths = (locator,)
        # Once any alias is sealed, enforce its immutable full digest even when
        # scanning an open alias after a duplicate copy changed the inode hint.
        sealed_hash = (
            _hash(fd, 0, info.st_size)
            if sealed or any(state.sealed_hash is not None for state in candidates)
            else None
        )
        conflict = any(state.conflict for state in candidates)
        if conflict:
            # A known conflicted variant remains blocked without manufacturing a new
            # generation on every rediscovery of its other locator.
            matching = [state for state in candidates if _matches(fd, state, info.st_size)]
            if matching:
                previous = max(matching, key=lambda state: state.generation)
        if previous:
            if legacy:
                paths = tuple(dict.fromkeys((*previous.discovered_paths, locator)))
            same_inode = (previous.device, previous.inode) == (info.st_dev, info.st_ino)
            matches = _matches(fd, previous, info.st_size)
            duplicate = previous.sealed_hash is not None and previous.sealed_hash == sealed_hash
            if matches and (same_inode or duplicate):
                action = "resume" if locator in previous.discovered_paths else "relocated"
                generation, offset = previous.generation, previous.committed_offset
                paths = tuple(dict.fromkeys((*previous.discovered_paths, locator)))
                # Preserve sealed invariants even when rediscovered via its open alias.
                sealed_hash = previous.sealed_hash or sealed_hash
                if legacy:
                    header_size = previous.header_size
            else:
                action = "generation"
                diagnostics.append("source_gap")
                if info.st_size < previous.last_size:
                    diagnostics.append("source_truncated")
                elif not same_inode:
                    diagnostics.append("source_replaced")
                else:
                    diagnostics.append("source_rewritten")
                # Sealed content and UUID header conflicts never allow normal progress.
                if not legacy and (
                    previous.sealed_hash is not None
                    or sealed
                    or not same_inode
                    or _hash(fd, 0, header_size) != previous.header_hash
                ):
                    conflict = True
                if not legacy:
                    diagnostics.append("segment_protocol_violation")
        # A reused locator with a new UUID is a new segment, but may strand unread bytes.
        for state in self._states.values():
            if locator in state.discovered_paths and state.key != key:
                if state.sealed_size is None or state.committed_offset < state.last_size:
                    diagnostics.append("source_gap")
                diagnostics.append("source_replaced")
        if sealed and os.pread(fd, 1, max(0, info.st_size - 1)) != b"\n":
            diagnostics.append("sealed_incomplete_tail")
        if conflict:
            action = "conflict"
            diagnostics.append("segment_uuid_conflict")
        state = Checkpoint(
            key,
            generation,
            version,
            paths,
            info.st_dev,
            info.st_ino,
            header_size,
            _hash(fd, 0, header_size),
            offset,
            _anchor(fd, offset),
            info.st_size,
            _anchor(fd, info.st_size),
            info.st_size if sealed_hash else None,
            sealed_hash,
            self._epoch,
            conflict,
        )
        return Decision(
            state, action, tuple(dict.fromkeys(diagnostics)), None if conflict else offset
        )
