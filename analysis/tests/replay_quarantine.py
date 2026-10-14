"""Synthetic V10/V16 demo retaining SQLite/fixtures only with --report-dir."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tempfile
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path

from store_fixture import COLLECTED, HANDOFF_SHA256, frozen_records

from sledra_analysis.ingest.reader import BoundedReader
from sledra_analysis.ingest.source import SourceKey, SourceTracker
from sledra_analysis.ingest.validation import EventValidator
from sledra_analysis.store.canonical import canonical_bytes
from sledra_analysis.store.raw import RawStore, SourceLocation, StoreCounts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-dir", type=Path, help="new private directory retaining SQLite and source bytes"
    )
    args = parser.parse_args()
    if args.report_dir is not None:
        args.report_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    context = (
        nullcontext(args.report_dir)
        if args.report_dir is not None
        else tempfile.TemporaryDirectory(prefix="sledra quarantine replay ")
    )
    with context as directory:
        root = Path(directory)
        records, _ = frozen_records(root / "shared")
        good = next(
            record.raw_bytes for record in records if record.location.source_kind == "segment"
        )
        original_event = json.loads(good)
        header = {
            "record_kind": "segment_header",
            "protocol_version": "1.0.0",
            "source_id": original_event["source"]["source_id"],
            "segment_uuid": "8c95fb7d-cf52-49ac-9815-444f1f37ffff",
            "writer_session": original_event["source"]["session_id"],
            "created_at": COLLECTED,
        }
        header_bytes = canonical_bytes(header) + b"\n"
        future = dict(original_event, schema_version="2.0.0")
        opaque = dict(
            original_event,
            schema_version="1.9.0",
            event_type="future.workflow_completed",
            event_id="00000000-0000-4000-8000-000000000012",
            payload={"reported_success": True},
        )
        bad = [
            b"{broken\n",
            b"\xff\n",
            good.replace(b'"event_id":', b'"event_id":"duplicate","event_id":'),
            canonical_bytes(future) + b"\n",
            b"X" * (1024 * 1024 + 1) + b"\n",
        ]
        source = root / "shared/injected.open.jsonl"
        source.write_bytes(
            header_bytes + good + b"".join(bad) + canonical_bytes(opaque) + b"\n" + good + good[:-1]
        )
        tracker = SourceTracker(root / "shared", "frozen-root")
        database = root / "facts.sqlite"
        with RawStore(database) as store:
            store.ingest(records)
            reader = BoundedReader(store, chunk_size=257, max_line_bytes=4096)

            def scan(path):
                with tracker.open(path.name) as lease:
                    result = reader.process(lease, locator=path.name, collected_at=COLLECTED)
                tracker.remember(result.checkpoint)  # model only; no durable checkpoint/ack
                return result

            first = scan(source)
            assert first.pending_tail_bytes == len(good) - 1
            assert len(first.quarantine_ids) == 5
            assert store.validation_results(opaque["event_id"])[0][2] == "opaque"
            with source.open("ab") as stream:
                stream.write(b"\nunfinished")
            second = scan(source)
            assert second.records == 1 and second.pending_tail_bytes == len(b"unfinished")
            sealed = source.with_name("injected.sealed.jsonl")
            source.rename(sealed)
            third = scan(sealed)
            assert len(third.quarantine_ids) == 1
            assert third.checkpoint.committed_offset == sealed.stat().st_size
            assert scan(sealed).records == 0
            for identity in (*first.quarantine_ids, *third.quarantine_ids):
                item = store.quarantine_record(identity)
                digest = hashlib.sha256()
                for block in store.quarantine_chunks(identity):
                    digest.update(block)
                assert digest.hexdigest() == item.raw_hash
            retained = store.reprocess(
                first.quarantine_ids[3], EventValidator(), attempted_at=COLLECTED
            )
            assert retained.status == "retained" and retained.reason == "unsupported_major"
            previous = root / "shared/previous-policy.jsonl"
            previous.write_bytes(good)
            location = SourceLocation(
                SourceKey("frozen-root", "previous-policy", "previous-file"),
                0,
                0,
                len(good),
                previous.name,
            )
            with io.BytesIO(good) as stream:
                identity = store.quarantine_stream(
                    stream,
                    location=location,
                    byte_size=len(good),
                    reason="unsupported_semantics",
                    validator_version="previous-policy-0.1",
                    collected_at=COLLECTED,
                )
            accepted = store.reprocess(identity, EventValidator(), attempted_at=COLLECTED)
            assert accepted.status == "accepted" and store.quarantine_bytes(identity) == good
            before = store.events()
            isolated = store.quarantines()
            counts = store.counts()
            assert counts == StoreCounts(20, 20, 24, 0)
            assert len(isolated) == 7
        with RawStore(database) as reopened:
            assert reopened.events() == before and reopened.quarantines() == isolated
        report = {
            "task": "SLD-12",
            "ok": True,
            "handoff_sha256": HANDOFF_SHA256,
            "frozen_facts": 19,
            "final_counts": asdict(counts),
            "quarantine": [asdict(item) for item in isolated],
            "open_scan": asdict(first),
            "completed_tail_scan": asdict(second),
            "sealed_scan": asdict(third),
            "reprocessing": [asdict(retained), asdict(accepted)],
            "database": "facts.sqlite",
            "database_sha256": hashlib.sha256(database.read_bytes()).hexdigest(),
            "position_model": "in memory only; no persistent checkpoint/ack/projection",
            "source_aliases": {"injected.open.jsonl": "injected.sealed.jsonl"},
        }
        if args.report_dir is not None:
            (root / "replay.json").write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {
                    "ok": True,
                    "frozen_facts": 19,
                    "final_counts": asdict(counts),
                    "quarantined_records": len(isolated),
                    "database_sha256": report["database_sha256"],
                }
            )
        )


if __name__ == "__main__":
    main()
