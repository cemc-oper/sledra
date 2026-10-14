"""Restore immutable synthetic M1 bytes for SQLite tests, never operational ingestion."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sledra_analysis.ingest.source import SourceKey, SourceTracker
from sledra_analysis.store.raw import RawRecord, SourceLocation

ROOT = Path(__file__).resolve().parents[2]
HANDOFF = ROOT / "tests/m1/results/handoff.json"
HANDOFF_SHA256 = "5c6c2bf40af052eedaf0edeb4ea02119f6d899824e086c8e26e210a436e282ef"
COLLECTED = "2026-10-14T00:00:00Z"


def frozen_records(shared: Path) -> tuple[list[RawRecord], dict[str, bytes]]:
    if hashlib.sha256(HANDOFF.read_bytes()).hexdigest() != HANDOFF_SHA256:
        raise ValueError("frozen handoff digest changed")
    handoff = json.loads(HANDOFF.read_bytes())
    shared.mkdir(mode=0o700)
    tracker = SourceTracker(shared, "frozen-root")
    records, original = [], {}
    for item in handoff["files"]:
        raw = item["text"].encode()
        if hashlib.sha256(raw).hexdigest() != item["sha256"]:
            raise ValueError("frozen source digest mismatch")
        original[item["path"]] = raw
        path = shared / item["path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        if item["path"].endswith(".jsonl"):
            with tracker.open(item["path"]) as lease:
                content = lease.read(0, len(raw))
                key = lease.decision.checkpoint.key
                lines = content.splitlines(keepends=True)
                offset = len(lines[0])
                for line in lines[1:]:
                    location = SourceLocation(key, 0, offset, offset + len(line), item["path"])
                    records.append(RawRecord(line, location, COLLECTED))
                    offset += len(line)
        else:
            value = json.loads(raw)
            if "outbox_event" in value:
                event = value["outbox_event"]
                key = SourceKey(
                    "frozen-root",
                    event["source"]["source_id"],
                    "registry:" + hashlib.sha256(item["path"].encode()).hexdigest(),
                )
                location = SourceLocation(key, 0, 0, len(raw), item["path"], "registry")
                records.append(RawRecord(raw, location, COLLECTED, "/outbox_event"))
    return records, original
