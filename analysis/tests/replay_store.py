"""Replay frozen M1 into SQLite. Retain the database only with --report-dir."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sqlite3
import tempfile
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path

from store_fixture import HANDOFF_SHA256, frozen_records

from sledra_analysis.store.raw import RawStore, StoreCounts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-dir", type=Path, help="new private directory retaining SQLite and inputs"
    )
    args = parser.parse_args()
    if args.report_dir is not None:
        args.report_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    context = (
        nullcontext(args.report_dir)
        if args.report_dir is not None
        else tempfile.TemporaryDirectory(prefix="sledra raw replay ")
    )
    with context as directory:
        root = Path(directory)
        records, original = frozen_records(root / "shared")
        database = root / "facts.sqlite"
        with RawStore(database) as store:
            first = store.ingest(records)
            assert store.counts() == StoreCounts(19, 19, 19, 0)
            random.Random(11).shuffle(records)
            repeated = store.ingest([*records, *reversed(records)])
            assert (repeated.new_events, repeated.new_versions, repeated.new_sources) == (0, 0, 0)
            before = store.events()
            for event in before:
                for version in event.versions:
                    assert (
                        hashlib.sha256(version.canonical_bytes).hexdigest()
                        == version.canonical_hash
                    )
                    for source in version.sources:
                        location = source.location
                        assert (
                            source.raw_bytes
                            == original[location.locator][location.byte_start : location.byte_end]
                        )
        with RawStore(database) as reopened:
            assert reopened.events() == before
            counts = reopened.counts()
        with sqlite3.connect(database) as reader:
            assert reader.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            assert reader.execute("PRAGMA foreign_key_check").fetchall() == []
        report = {
            "task": "SLD-11",
            "ok": True,
            "handoff_sha256": HANDOFF_SHA256,
            "input_files": 22,
            "segment_events": 11,
            "outbox_events": 8,
            "first_batch": asdict(first),
            "repeated_batch": asdict(repeated),
            "reopened_counts": asdict(counts),
            "projections": "not implemented",
            "database": "facts.sqlite",
            "database_sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
            "sqlite": sqlite3.sqlite_version,
        }
        if args.report_dir is not None:
            (root / "replay.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report))


if __name__ == "__main__":
    main()
