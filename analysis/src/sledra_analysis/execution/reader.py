"""Read-only, bounded file scans. No registration, repair, deduplication or storage."""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import os
import re
import stat
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .codec import (
    MAX_RECORD_BYTES,
    UUID,
    ContractError,
    comparable,
    equivalent,
    execution_path,
    partition_date,
    validate,
)


@dataclass(frozen=True)
class Source:
    path: str  # Root-relative path, never raw record text.
    line: int | None = None  # One-based for JSONL; None for a whole file/directory.
    offset: int = 0
    length: int = 0  # Physical bytes, including LF when present.


@dataclass(frozen=True)
class Fact:
    kind: str
    data: dict
    source: Source


@dataclass(frozen=True)
class ReadIssue:
    code: str
    source: Source
    record_id: str | None = None


@dataclass(frozen=True)
class RunReference:
    status: str  # found / missing / conflicting / unavailable
    records: tuple[Fact, ...]


@dataclass(frozen=True)
class RunLookup:
    references: dict[str, RunReference]
    issues: tuple[ReadIssue, ...]


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


def _root_fd(path: Path) -> int:
    if not path.is_absolute():
        raise ContractError("root_invalid")
    # Open each component without following links, including ancestors. All
    # subsequent operations are relative to these directory descriptors.
    try:
        fd = os.open("/", _DIR_FLAGS)
    except OSError:
        raise ContractError("root_unavailable") from None
    try:
        for part in Path(os.path.abspath(path)).parts[1:]:
            child = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except OSError:
        os.close(fd)
        raise ContractError("root_unavailable") from None


@contextmanager
def _regular(parent: int, name: str):
    fd = os.open(name, _FILE_FLAGS, dir_fd=parent)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("regular unlinked-alias-free file required")
        file = os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise
    with file:
        yield file, info.st_size


def _date(value: str | dt.date) -> dt.date:
    if type(value) is dt.date:
        return value
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        raise ContractError("invalid_window")
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise ContractError("invalid_window") from None


class FileReader:
    """Open one compatible root. Use with/close; generators retain only open FDs.

    Event windows are explicit and inclusive. Pass (None, None) to scan all
    retained record dates. Ordering is filesystem order, never causal order.
    """

    def __init__(self, root: str | Path):
        self._fd = _root_fd(Path(root))
        try:
            for name in ("registry", "documents", "contexts", "spool"):
                try:
                    os.stat(name, dir_fd=self._fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                raise ContractError("root_incompatible")
            with _regular(self._fd, "sledra.json") as (file, _):
                raw = file.read(MAX_RECORD_BYTES + 1)
            try:
                self._config = validate("root", raw)
            except ContractError:
                raise ContractError("root_incompatible") from None
        except BaseException as exc:
            self.close()
            if isinstance(exc, OSError):
                raise ContractError("root_unavailable") from None
            raise

    @property
    def config(self) -> dict:
        return copy.deepcopy(self._config)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def _duplicate(self) -> int:
        if self._fd is None:
            raise ContractError("reader_closed")
        return os.dup(self._fd)

    def _directories(self, parent: int, kind: str, window) -> Iterator[tuple[int, str] | ReadIssue]:
        """Yield one open date descriptor at a time; never recursively walk."""
        try:
            base = os.open(kind, _DIR_FLAGS, dir_fd=parent)
        except FileNotFoundError:
            return
        except OSError:
            yield ReadIssue("input_unavailable", Source(kind))
            return
        try:
            with os.scandir(base) as entries:
                for entry in entries:
                    date = entry.name
                    try:
                        if re.fullmatch(r"[0-9]{8}", date) is None:
                            raise ValueError
                        day = dt.date(int(date[:4]), int(date[4:6]), int(date[6:8]))
                    except ValueError:
                        yield ReadIssue("invalid_layout", Source(f"{kind}/{date}"))
                        continue
                    if window and not window[0] <= day <= window[1]:
                        continue
                    try:
                        fd = os.open(date, _DIR_FLAGS, dir_fd=base)
                    except OSError:
                        yield ReadIssue("input_unavailable", Source(f"{kind}/{date}"))
                        continue
                    try:
                        yield fd, f"{kind}/{date}"
                    finally:
                        os.close(fd)
        except OSError:
            yield ReadIssue("input_unavailable", Source(kind))
        finally:
            os.close(base)

    def iter_events(
        self, record_from: str | dt.date | None, record_through: str | dt.date | None
    ) -> Iterator[Fact | ReadIssue]:
        if record_from is None and record_through is None:
            window = None
        else:
            window = (_date(record_from), _date(record_through))
            if window[0] > window[1]:
                raise ContractError("invalid_window")
        parent = self._duplicate()
        try:
            for item in self._directories(parent, "executions", window):
                if isinstance(item, ReadIssue):
                    yield item
                    continue
                fd, path = item
                try:
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            bucket = entry.name
                            if bucket not in "0123456789abcdef" or len(bucket) != 1:
                                yield ReadIssue("invalid_layout", Source(f"{path}/{bucket}"))
                                continue
                            try:
                                child = os.open(bucket, _DIR_FLAGS, dir_fd=fd)
                            except OSError:
                                yield ReadIssue("input_unavailable", Source(f"{path}/{bucket}"))
                                continue
                            try:
                                yield from self._event_files(child, f"{path}/{bucket}", bucket)
                            except OSError:
                                yield ReadIssue("input_unavailable", Source(f"{path}/{bucket}"))
                            finally:
                                os.close(child)
                except OSError:
                    yield ReadIssue("input_unavailable", Source(path))
        finally:
            os.close(parent)

    def _event_files(self, fd: int, path: str, bucket: str) -> Iterator[Fact | ReadIssue]:
        with os.scandir(fd) as entries:
            for entry in entries:
                name = entry.name
                identity = name.removesuffix(".jsonl")
                source = Source(f"{path}/{name}")
                if not name.endswith(".jsonl") or UUID.fullmatch(identity) is None:
                    yield ReadIssue("invalid_layout", source)
                elif hashlib.sha256(identity.encode()).hexdigest()[0] != bucket:
                    yield ReadIssue("bucket_mismatch", source, identity)
                else:
                    yield from self._events(fd, name, source.path, identity)

    def _events(
        self, parent: int, name: str, path: str, identity: str
    ) -> Iterator[Fact | ReadIssue]:
        offset, number = 0, 1
        try:
            with _regular(parent, name) as (file, size):
                if size == 0:
                    yield ReadIssue("incomplete_tail", Source(path, 1), identity)
                while offset < size:
                    raw = file.readline(min(MAX_RECORD_BYTES + 2, size - offset))
                    length = len(raw)
                    ended = raw.endswith(b"\n")
                    # Discard an oversized physical line in bounded chunks, then
                    # resume at its next line; never load an entire history.
                    while not ended and offset + length < size:
                        chunk = file.readline(min(MAX_RECORD_BYTES + 2, size - offset - length))
                        if not chunk:
                            break
                        length += len(chunk)
                        ended = chunk.endswith(b"\n")
                    source = Source(path, number, offset, length)
                    if offset + length < size and not ended:
                        yield ReadIssue("source_changed", source, identity)
                        break
                    if not ended:
                        yield ReadIssue("incomplete_tail", source, identity)
                    elif length > MAX_RECORD_BYTES + 1:
                        yield ReadIssue("limit_exceeded", source, identity)
                    else:
                        try:
                            event = validate("event", raw[:-1])
                            if event["execution_id"] != identity:
                                raise ContractError("execution_mismatch")
                            if (
                                event["name"] == "execution.started"
                                and execution_path(identity, event["occurred_at"], self._config)
                                != path
                            ):
                                raise ContractError("partition_mismatch")
                            yield Fact("event", event, source)
                        except ContractError as exc:
                            yield ReadIssue(exc.code, source, identity)
                    offset += length
                    number += 1
        except OSError:
            yield ReadIssue("input_unavailable", Source(path, number, offset), identity)

    def iter_runs(self, run_ids: Collection[str] | None = None) -> Iterator[Fact | ReadIssue]:
        selected = None if run_ids is None else frozenset(run_ids)
        if selected is not None and any(UUID.fullmatch(i) is None for i in selected):
            raise ContractError("schema_invalid")
        if selected == frozenset():
            return
        parent = self._duplicate()
        try:
            for item in self._directories(parent, "runs", None):
                if isinstance(item, ReadIssue):
                    yield item
                    continue
                fd, path = item
                try:
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            name = entry.name
                            identity = name.removesuffix(".json")
                            source = Source(f"{path}/{name}")
                            if not name.endswith(".json") or UUID.fullmatch(identity) is None:
                                yield ReadIssue("invalid_layout", source)
                            elif selected is None or identity in selected:
                                yield self._run(fd, name, source.path, identity)
                except OSError:
                    yield ReadIssue("input_unavailable", Source(path))
        finally:
            os.close(parent)

    def _run(self, parent: int, name: str, path: str, identity: str) -> Fact | ReadIssue:
        source = Source(path)
        try:
            with _regular(parent, name) as (file, size):
                raw = file.read(MAX_RECORD_BYTES + 1)
            source = Source(path, length=size)
            if len(raw) < min(size, MAX_RECORD_BYTES + 1):
                raise ContractError("source_changed")
            record = validate("run", raw)
            if record["run_id"] != identity:
                raise ContractError("run_mismatch")
            if record["namespace"] != self._config["namespace"]:
                raise ContractError("namespace_mismatch")
            if partition_date(record["created_at"], self._config) != path.split("/")[1]:
                raise ContractError("partition_mismatch")
            return Fact("run", record, source)
        except ContractError as exc:
            return ReadIssue(exc.code, source, identity)
        except OSError:
            return ReadIssue("input_unavailable", source, identity)

    def resolve_runs(self, run_ids: Collection[str]) -> RunLookup:
        selected = frozenset(run_ids)
        records = {identity: [] for identity in selected}
        issues = []
        for item in self.iter_runs(selected):
            if isinstance(item, ReadIssue):
                issues.append(item)
            else:
                records[item.data["run_id"]].append(item)
        references = {}
        for identity, candidates in records.items():
            values = [comparable(item.data, "run") for item in candidates]
            if values and any(not equivalent(values[0], other) for other in values[1:]):
                status = "conflicting"
            elif any(item.record_id in (None, identity) for item in issues):
                status = "unavailable"
            else:
                status = "found" if candidates else "missing"
            references[identity] = RunReference(status, tuple(candidates))
        return RunLookup(references, tuple(issues))
