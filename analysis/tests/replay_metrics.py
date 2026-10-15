"""Retain SLD-15 frozen replay plus mixed-sample and late-end timing golden reports."""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import random
import tempfile
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path

from store_fixture import COLLECTED, HANDOFF_SHA256, ROOT, frozen_records

from sledra_analysis.ingest.source import SourceKey
from sledra_analysis.metrics.engine import MetricsEngine
from sledra_analysis.projections.documents import DocumentRecord
from sledra_analysis.projections.engine import ProjectionEngine
from sledra_analysis.store.canonical import canonical_bytes, parse_json
from sledra_analysis.store.raw import RawRecord, RawStore, SourceLocation
from sledra_analysis.store.recovery import RecoveryStore

AS_OF = "2026-10-15T00:00:00Z"
PUBLISHED = "2026-10-15T01:00:00Z"


def synthetic_inputs():
    bundle = parse_json((ROOT / "spec/fixtures/1.0.0/valid-bundle.json").read_bytes())
    catalog = parse_json((ROOT / "spec/1.0.0/event-types.json").read_bytes())
    context = bundle["attempt_context"]
    records, events = [], []
    for interval, result, duration, sides in (
        ("success", "success", 10, (0, 1)),
        ("failure", "failure", 30, (0, 1)),
        ("end-only", "success", None, (1,)),
        ("late", "unknown", None, (0, 1)),
    ):
        for side in sides:
            event = copy.deepcopy(bundle["events"][0])
            name = "stage.ended" if side else "stage.started"
            event.update(
                event_id=f"00000000-0000-4000-8000-{len(events) + 1:012x}",
                event_type=name,
                association={k: context[k] for k in ("run_id", "task_instance_id", "attempt_id")},
                occurred_at="2026-10-14T23:59:58Z" if not side else "2026-10-15T00:00:00Z",
                time_quality={
                    "basis": "producer_clock",
                    "clock_error_bound_ns": 0,
                    "precision_ns": 0,
                },
                source={
                    "source_id": "synthetic-timing",
                    "session_id": "session-15",
                    "kind": "direct",
                },
                payload={"stage_key": "forecast", "interval_id": interval},
            )
            event["producer"]["role"] = catalog[name]["roles"][0]
            if side:
                event["payload"]["result"] = result
                if duration is not None:
                    event["payload"]["duration_ns"] = duration
            raw = canonical_bytes(event) + b"\n"
            records.append(
                RawRecord(
                    raw,
                    SourceLocation(
                        SourceKey("sld-15-demo", "synthetic-timing", "intervals"),
                        0,
                        0,
                        len(raw),
                        event["event_id"] + ".jsonl",
                    ),
                    COLLECTED,
                )
            )
            events.append(event)
    documents = [
        DocumentRecord(canonical_bytes(bundle[k]), k + ".json", COLLECTED)
        for k in ("definition_manifest", "run_context", "attempt_context")
    ]
    return records, documents, events


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-dir", type=Path, help="new private directory; never overwrite")
    args = parser.parse_args()
    if args.report_dir:
        args.report_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    with (
        nullcontext(args.report_dir)
        if args.report_dir
        else tempfile.TemporaryDirectory(prefix="sledra timing replay ") as directory
    ):
        root = Path(directory).absolute()
        frozen, sources = frozen_records(root / "shared")
        with RawStore(root / "frozen.sqlite") as store:
            metrics = MetricsEngine(store)
            ProjectionEngine(store).import_documents(
                [
                    DocumentRecord(raw, name, COLLECTED)
                    for name, raw in sources.items()
                    if name.endswith(".json")
                    and parse_json(raw).get("kind") == "definition_manifest"
                ]
            )
            store.ingest(frozen)
            frozen_report = metrics.publish(report_as_of=AS_OF, published_at=PUBLISHED)
            processes = [
                r for r in frozen_report.data["intervals"] if r["metric"] == "process_execution"
            ]
            assert len(processes) == 4
            assert all(r["duration_ns"] is None for r in processes)
            assert any(r["pairing"] == "unavailable" for r in processes)
        records, documents, events = synthetic_inputs()
        with RawStore(root / "facts.sqlite") as store:
            metrics = MetricsEngine(store)
            ProjectionEngine(store).import_documents(documents)
            store.ingest(records[:-1])
            first = metrics.publish(report_as_of=AS_OF, published_at=PUBLISHED)
            stat = next(s for s in first.data["statistics"] if s["stratum"]["metric"] == "stage")
            assert (
                stat["total"],
                stat["missing_start"],
                stat["right_censored"],
                stat["statistics"]["n"],
                stat["statistics"]["failure_n"],
            ) == (4, 1, 1, 2, 1)
            assert stat["statistics"]["mean_ns"] == {"numerator": "20", "denominator": "1"}
            store.ingest(records)
            second = metrics.publish(report_as_of=AS_OF, published_at=PUBLISHED)
            assert first.report_id != second.report_id and metrics.report(first.report_id) == first
            assert metrics.publish(report_as_of=AS_OF, published_at=PUBLISHED) == second
            recovery = RecoveryStore(root / "recovery")
            backup = recovery.backup(store)
            recovery.restore(backup, root / "restored.sqlite")
            with RawStore(root / "restored.sqlite") as restored:
                assert MetricsEngine(restored).report() == second
            report = {
                "task": "SLD-15",
                "matrices": ["V07", "V22"],
                "handoff_sha256": HANDOFF_SHA256,
                "frozen_sources": len(sources),
                "frozen_processes": len(processes),
                "frozen_exact_duration_n": 0,
                "mixed_golden": stat,
                "before_report": first.report_id,
                "after_report": second.report_id,
                "input_hash": second.input_hash,
                "counts": asdict(store.counts()),
                "status": metrics.status(),
                "backup_restore": "pass",
                "business_jobs_executed": 0,
            }
        with RawStore(root / "shuffled.sqlite") as store:
            metrics = MetricsEngine(store)
            ProjectionEngine(store).import_documents(documents)
            shuffled = records + records
            random.Random(15).shuffle(shuffled)
            store.ingest(shuffled)
            actual = metrics.publish(report_as_of=AS_OF, published_at=PUBLISHED)
            assert actual.report_id == second.report_id and actual.data == second.data
            report["shuffled_duplicate_replay"] = {"seed": 15, "same_report_id_and_data": True}
        assert all((root / "shared" / name).read_bytes() == raw for name, raw in sources.items())
        report["frozen_bytes_unchanged"] = True
        report["database_sha256"] = hashlib.sha256((root / "facts.sqlite").read_bytes()).hexdigest()
        if args.report_dir:
            for name, data in (
                ("frozen-report.json", asdict(frozen_report)),
                ("mixed-incomplete.json", asdict(first)),
                ("late-end.json", asdict(second)),
                ("synthetic-events.json", events),
                ("replay.json", report),
            ):
                path = root / name
                path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
                path.chmod(0o600)
        print(
            json.dumps(
                {
                    "frozen_processes": 4,
                    "mixed_samples": 4,
                    "initial_complete_n": 2,
                    "initial_failure_n": 1,
                    "late_endpoint_revision": True,
                    "shuffle_and_backup_restore": "pass",
                }
            )
        )


if __name__ == "__main__":
    main()
