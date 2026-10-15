"""V12/V21 synthetic transaction/confirmation/restore demo; never operational cleanup."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path

from store_fixture import COLLECTED, HANDOFF_SHA256, frozen_records

from sledra_analysis.ingest.transaction import AtomicIngestor
from sledra_analysis.store.canonical import canonical_bytes
from sledra_analysis.store.raw import RawStore, StoreError
from sledra_analysis.store.recovery import RecoveryStore

LATER = "2026-10-15T00:00:00Z"


def facts(store):
    return [
        (event.event_id, event.conflicted, [v.canonical_hash for v in event.versions])
        for event in store.events()
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-dir", type=Path, help="new private directory preserving all evidence"
    )
    args = parser.parse_args()
    if args.report_dir is not None:
        args.report_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    context = (
        nullcontext(args.report_dir)
        if args.report_dir
        else tempfile.TemporaryDirectory(prefix="sledra recovery replay ")
    )
    with context as directory:
        root = Path(directory).absolute()
        _, sources = frozen_records(root / "shared")
        recovery = RecoveryStore(root / "recovery")
        with RawStore(root / "facts.sqlite") as store:
            ingest = AtomicIngestor(store, root / "shared", "frozen-root")
            for locator, raw in sources.items():
                if locator.endswith(".jsonl"):
                    ingest.scan(locator, collected_at=COLLECTED)
                elif "outbox_event" in json.loads(raw):
                    ingest.registry(
                        locator,
                        source_id=json.loads(raw)["outbox_event"]["source"]["source_id"],
                        collected_at=COLLECTED,
                    )
            assert store.counts().events == 19
            baseline = asdict(store.counts())
            stale_backup = recovery.backup(store)
            first = json.loads(store.events()[0].versions[0].canonical_bytes)
            conflict = dict(first, recorded_at=LATER)
            opaque = dict(
                first,
                event_id="00000000-0000-4000-8000-000000000013",
                event_type="future.critical",
                schema_version="1.9.0",
                payload={"observed": True},
            )
            header = {
                "record_kind": "segment_header",
                "protocol_version": "1.0.0",
                "source_id": "recovery-test",
                "segment_uuid": "8c95fb7d-cf52-49ac-9815-444f1f37ff13",
                "writer_session": "recovery-demo",
                "created_at": LATER,
            }
            raw = (
                canonical_bytes(header)
                + b"\n"
                + canonical_bytes(conflict)
                + b"\n{bad\n\xff\n"
                + canonical_bytes(opaque)
                + b"\npartial"
            )
            source = root / "shared/recovery.sealed.jsonl"
            source.write_bytes(raw)
            result = ingest.scan(source.name, collected_at=LATER)
            expected, queue = facts(store), store.projection_work()
            receipt = recovery.acknowledge(
                store, root / "shared", "frozen-root", source.name, acknowledged_at=LATER
            )
            recovery.verify_receipt(receipt)
            source.unlink()  # Explicit synthetic V21 source-retention simulation only.
            try:
                recovery.restore(stale_backup, root / "stale.sqlite")
            except StoreError:
                stale_refused = True
            else:
                raise AssertionError("stale backup unexpectedly accepted")
            recovery.restore(receipt["backup"], root / "restored.sqlite")
            with RawStore(root / "restored.sqlite") as restored:
                assert facts(restored) == expected
                assert restored.projection_work() == queue
                assert [restored.quarantine_bytes(q) for q in result.quarantine_ids] == [
                    b"{bad\n",
                    b"\xff\n",
                    b"partial",
                ]
                source.write_bytes((recovery.root / receipt["raw_archive"]).read_bytes())
                replay = AtomicIngestor(restored, root / "shared", "frozen-root").scan(
                    source.name, collected_at=LATER
                )
                assert replay.records == 0
                assert facts(restored) == expected
                restored_counts = asdict(restored.counts())
            for locator, original in sources.items():
                assert (root / "shared" / locator).read_bytes() == original
            report = {
                "task": "SLD-13",
                "matrices": ["V12", "V21"],
                "handoff_sha256": HANDOFF_SHA256,
                "baseline_counts": baseline,
                "counts": asdict(store.counts()),
                "restored_counts": restored_counts,
                "quarantines": [asdict(q) for q in store.quarantines()],
                "projection_queue_items": len(queue),
                "watermark": store.watermark(),
                "receipt": receipt,
                "checkpoint": asdict(result.checkpoint),
                "stale_backup_refused": stale_refused,
                "replayed_records_after_restore": replay.records,
                "synthetic_source_removal_only": True,
                "business_jobs_executed": 0,
                "frozen_source_files": len(sources),
                "all_frozen_source_bytes_unchanged": True,
                "database_sha256": hashlib.sha256((root / "facts.sqlite").read_bytes()).hexdigest(),
            }
        if args.report_dir:
            (root / "replay.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            )
        print(
            json.dumps(
                {
                    "counts": report["counts"],
                    "restored": restored_counts,
                    "stale_backup_refused": True,
                    "replayed_records": 0,
                }
            )
        )


if __name__ == "__main__":
    main()
