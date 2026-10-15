"""Immutable raw archives, consistent SQLite snapshots and post-commit receipts.

All paths are in an explicitly configured, private management recovery directory.
These APIs neither enable retention policy nor delete a task-side source.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import asdict
from pathlib import Path

from sledra_analysis.ingest.source import SourceError, SourceTracker, _open
from sledra_analysis.store.ingestion import checkpoint_key, decode_checkpoint
from sledra_analysis.store.raw import RawStore, StoreError, _timestamp


def _sync(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with _open(path.parent, path.name) as fd:
        while block := os.read(fd, 65536):
            digest.update(block)
    return digest.hexdigest()


def _json(value: dict) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def _publish(path: Path, chunks, *, expected_hash: str | None = None) -> str:
    """Same-directory fsync + no-replace publication; identical retries are safe."""
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    temporary = Path(temporary)
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, "wb") as stream:
            for block in chunks:
                stream.write(block)
                digest.update(block)
            stream.flush()
            os.fsync(stream.fileno())
        actual = digest.hexdigest()
        if expected_hash is not None and actual != expected_hash:
            raise StoreError("archive bytes do not match sealed digest")
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            if _hash(path) != actual:
                raise StoreError("immutable publication conflicts with existing bytes") from None
        _sync(path.parent)
        return actual
    finally:
        temporary.unlink(missing_ok=True)


class RecoveryStore:
    """Single management writer owns this recovery namespace and its durable media."""

    def __init__(self, root: Path):
        self.root = Path(root).absolute()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        _sync(self.root)
        _sync(self.root.parent)
        for name in ("raw", "backups", "acknowledgements"):
            path = self.root / name
            path.mkdir(mode=0o700, exist_ok=True)
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            os.close(fd)
        _sync(self.root)

    def _stage(self, name: str) -> None:
        """Internal fault-injection seam."""

    def _read_json(self, locator: str) -> dict:
        with _open(self.root, locator) as fd:
            raw = os.read(fd, 1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise StoreError("recovery metadata exceeds budget")
            value = json.loads(raw)
        if not isinstance(value, dict):
            raise StoreError("invalid recovery metadata")
        return value

    def backup(self, store: RawStore) -> dict:
        """Snapshot raw, isolation, checkpoints, archive ledger and queue together."""
        if store._active:
            raise StoreError("backup requires a committed database")
        fd, temporary = tempfile.mkstemp(prefix=".snapshot-", dir=self.root / "backups")
        os.close(fd)
        temporary = Path(temporary)
        try:
            with sqlite3.connect(temporary) as target:
                store._db().backup(target)
                if (
                    target.execute("PRAGMA quick_check").fetchall() != [("ok",)]
                    or target.execute("PRAGMA foreign_key_check").fetchall()
                ):
                    raise StoreError("inconsistent snapshot")
                watermark = {
                    "lineage": target.execute("SELECT lineage FROM ingestion_identity").fetchone()[
                        0
                    ],
                    "commit_id": target.execute(
                        "SELECT coalesce(max(commit_id), 0) FROM ingestion_commits"
                    ).fetchone()[0],
                    "queue_id": target.execute(
                        "SELECT coalesce(max(work_id), 0) FROM projection_work_queue"
                    ).fetchone()[0],
                }
            digest = _hash(temporary)
            locator = f"backups/{digest}.sqlite"
            with temporary.open("rb") as stream:
                _publish(
                    self.root / locator, iter(lambda: stream.read(65536), b""), expected_hash=digest
                )
            manifest = {
                "kind": "sledra_backup",
                "version": 1,
                "sha256": digest,
                "database": locator,
                "watermark": watermark,
            }
            self._stage("after_snapshot")
            _publish(self.root / f"backups/{digest}.json", (_json(manifest),))
            self._stage("after_backup_manifest")
            return manifest
        finally:
            temporary.unlink(missing_ok=True)

    def acknowledge(
        self,
        store: RawStore,
        source_root: Path,
        source_root_id: str,
        locator: str,
        *,
        acknowledged_at: str,
    ) -> dict:
        """Acknowledge only a fully committed immutable sealed segment after backup."""
        if store._active or not _timestamp(acknowledged_at):
            raise StoreError("acknowledgement requires committed state and valid time")
        tracker = SourceTracker(source_root, source_root_id, store.checkpoints(source_root_id))
        with tracker.open(locator) as lease:
            state = lease.decision.checkpoint
            if (
                not locator.endswith(".sealed.jsonl")
                or state.conflict
                or state.sealed_hash is None
                or state.committed_offset != state.sealed_size
            ):
                raise SourceError("only fully committed sealed sources may be acknowledged")
            key = checkpoint_key(state)
            row = (
                store._db()
                .execute(
                    "SELECT state, commit_id FROM checkpoints WHERE source_root_id=? AND source_id=? AND segment_uuid=? AND generation=?",
                    key,
                )
                .fetchone()
            )
            if row is None:
                raise StoreError("source has no committed checkpoint")
            persisted = decode_checkpoint(row[0])
            if (
                persisted.conflict
                or persisted.sealed_hash != state.sealed_hash
                or persisted.committed_offset != state.sealed_size
            ):
                raise StoreError("source differs from committed sealed checkpoint; ingest it first")
            receipt_name = hashlib.sha256(
                _json({"key": asdict(state.key), "generation": state.generation})
            ).hexdigest()
            receipt_path = self.root / "acknowledgements" / f"{receipt_name}.json"
            if receipt_path.exists():
                receipt = self._read_json(str(receipt_path.relative_to(self.root)))
                if (
                    receipt["raw_hash"] != state.sealed_hash
                    or receipt["byte_size"] != state.sealed_size
                    or receipt["backup"]["watermark"]["lineage"] != store.watermark()["lineage"]
                ):
                    raise StoreError("acknowledgement conflicts with source")
                self.verify_receipt(receipt)
                lease._verify()
                _sync(receipt_path.parent)
                return receipt
            raw_locator = f"raw/{state.sealed_hash}.jsonl"
            with lease.archive_stream() as stream:
                _publish(self.root / raw_locator, stream.chunks(), expected_hash=state.sealed_hash)
            self._stage("after_raw_archive")
            with store._ingestion_transaction(acknowledged_at) as commit:
                store._db().execute(
                    "INSERT INTO raw_archives VALUES(?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT DO NOTHING",
                    (*key, state.sealed_hash, state.sealed_size, raw_locator, commit),
                )
            self._stage("after_archive_commit")
            backup = self.backup(store)
            receipt = {
                "kind": "sledra_ingest_ack",
                "version": 1,
                "key": asdict(state.key),
                "generation": state.generation,
                "raw_hash": state.sealed_hash,
                "byte_size": state.sealed_size,
                "committed_offset": persisted.committed_offset,
                "raw_archive": raw_locator,
                "checkpoint_commit": row[1],
                "backup": backup,
                "acknowledged_at": acknowledged_at,
            }
            lease._verify()
            self.verify_receipt(receipt)
            lease._verify()
            self._stage("before_ack")
            _publish(receipt_path, (_json(receipt),))
            self._stage("after_ack")
            return receipt

    def _verified_backup(self, manifest: dict) -> Path:
        digest = manifest["sha256"]
        if (
            manifest.get("kind") != "sledra_backup"
            or manifest.get("version") != 1
            or manifest["database"] != f"backups/{digest}.sqlite"
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
        ):
            raise StoreError("invalid backup manifest")
        path = self.root / manifest["database"]
        if _hash(path) != digest:
            raise StoreError("backup digest mismatch")
        return path

    def verify_receipt(self, receipt: dict) -> None:
        if receipt.get("kind") != "sledra_ingest_ack" or receipt.get("version") != 1:
            raise StoreError("not a durable ingestion acknowledgement")
        raw_hash = receipt["raw_hash"]
        if (
            len(raw_hash) != 64
            or any(c not in "0123456789abcdef" for c in raw_hash)
            or receipt["raw_archive"] != f"raw/{raw_hash}.jsonl"
        ):
            raise StoreError("invalid raw archive locator")
        raw = self.root / receipt["raw_archive"]
        if _hash(raw) != raw_hash or raw.stat().st_size != receipt["byte_size"]:
            raise StoreError("raw archive digest/size mismatch")
        path = self._verified_backup(receipt["backup"])
        with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as db:
            self._check_watermark(db, receipt["backup"])
            self._covers_receipt(db, receipt)

    def _covers_receipt(self, db, receipt):
        key = receipt["key"]
        fields = (
            key["source_root_id"],
            key["source_id"],
            key["segment_uuid"],
            receipt["generation"],
        )
        lineage = db.execute("SELECT lineage FROM ingestion_identity").fetchone()[0]
        row = db.execute(
            "SELECT c.state, c.commit_id, a.raw_hash, a.byte_size, a.archive_locator FROM checkpoints c "
            "JOIN raw_archives a USING(source_root_id, source_id, segment_uuid, generation) "
            "WHERE c.source_root_id=? AND c.source_id=? AND c.segment_uuid=? AND c.generation=?",
            fields,
        ).fetchone()
        if row is None:
            raise StoreError("backup predates an acknowledged checkpoint/archive")
        state = decode_checkpoint(row[0])
        if (
            lineage != receipt["backup"]["watermark"]["lineage"]
            or state.committed_offset != receipt["committed_offset"]
            or state.committed_offset != receipt["byte_size"]
            or state.sealed_hash != receipt["raw_hash"]
            or row[1] < receipt["checkpoint_commit"]
            or row[2:] != (receipt["raw_hash"], receipt["byte_size"], receipt["raw_archive"])
        ):
            raise StoreError("backup does not cover acknowledged source")

    def _check_watermark(self, db, manifest):
        watermark = {
            "lineage": db.execute("SELECT lineage FROM ingestion_identity").fetchone()[0],
            "commit_id": db.execute(
                "SELECT coalesce(max(commit_id), 0) FROM ingestion_commits"
            ).fetchone()[0],
            "queue_id": db.execute(
                "SELECT coalesce(max(work_id), 0) FROM projection_work_queue"
            ).fetchone()[0],
        }
        if manifest["watermark"] != watermark:
            raise StoreError("backup watermark mismatch")

    def restore(self, manifest: dict, destination: Path) -> None:
        """Restore to a new DB, refusing any snapshot behind retained receipts.

        The configured recovery namespace must contain *all* published receipts.
        Omitted/lost external receipts cannot be inferred by an offline restore.
        """
        source = self._verified_backup(manifest)
        with sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True) as db:
            if (
                db.execute("PRAGMA quick_check").fetchall() != [("ok",)]
                or db.execute("PRAGMA foreign_key_check").fetchall()
            ):
                raise StoreError("invalid backup database")
            self._check_watermark(db, manifest)
            for path in sorted((self.root / "acknowledgements").iterdir()):
                if path.name.startswith(".pending-"):
                    continue
                receipt = self._read_json(str(path.relative_to(self.root)))
                self.verify_receipt(receipt)
                self._covers_receipt(db, receipt)
        destination = Path(destination)
        if destination.exists() or destination.is_symlink():
            raise StoreError("restore destination must be new")
        with source.open("rb") as stream:
            _publish(
                destination, iter(lambda: stream.read(65536), b""), expected_hash=manifest["sha256"]
            )
