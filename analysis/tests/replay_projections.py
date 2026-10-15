"""Synthetic SLD-14 replay: recovered -> rerunning, immutable revisions and cache rebuild."""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import tempfile
from contextlib import nullcontext
from dataclasses import asdict
from pathlib import Path

from store_fixture import COLLECTED, HANDOFF_SHA256, frozen_records

from sledra_analysis.projections.documents import DocumentRecord
from sledra_analysis.projections.engine import ProjectionEngine
from sledra_analysis.projections.reducer import REDUCER_VERSION, RULE_VERSION
from sledra_analysis.store.canonical import parse_json
from sledra_analysis.store.raw import RawStore
from sledra_analysis.store.recovery import RecoveryStore

LATER = "2026-10-15T00:00:00Z"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-dir", type=Path, help="new private directory retaining fixtures and SQLite"
    )
    args = parser.parse_args()
    if args.report_dir:
        args.report_dir.mkdir(parents=True, mode=0o700, exist_ok=False)
    context = (
        nullcontext(args.report_dir)
        if args.report_dir
        else tempfile.TemporaryDirectory(prefix="sledra projection replay ")
    )
    with context as directory:
        root = Path(directory).absolute()
        records, sources = frozen_records(root / "shared")
        late = parse_json(sources["contexts/late-attempt.json"])
        late_record = next(
            r
            for r in records
            if r.event_pointer
            and parse_json(r.raw_bytes)["outbox_event"]["association"].get("attempt_id")
            == late["attempt_id"]
        )
        baseline = [r for r in records if r is not late_record]
        documents = [
            DocumentRecord(raw, name, COLLECTED)
            for name, raw in sources.items()
            if name.endswith(".json") and parse_json(raw).get("kind") == "definition_manifest"
        ]
        with RawStore(root / "facts.sqlite") as store:
            engine = ProjectionEngine(store)
            engine.import_documents(documents)
            store.ingest(baseline)
            first = engine.rebuild(published_at=LATER)
            first_summary = engine.get("instances", late["task_instance_id"])
            assert first_summary["execution"]["state"] == "recovered"
            assert engine.get("runs", late["run_id"])["lifecycle"]["state"] == "finish_declared"
            store.ingest(baseline)
            assert engine.rebuild(published_at=LATER) == first
            store.ingest([late_record])
            assert engine.status()["state"] == "pending"
            second = engine.rebuild(published_at=LATER)
            assert (
                engine.get("instances", late["task_instance_id"])["execution"]["state"]
                == "rerunning"
            )
            assert engine.get("runs", late["run_id"])["lifecycle"]["state"] == "needs_finalization"
            assert engine.revision(first.revision_id) == first
            assert first.revision_id != second.revision_id
            # Explicit loss of only the derived current cache in this private fixture.
            with store.transaction():
                store._db().execute("DELETE FROM projection_heads")
                store._db().execute(
                    "DELETE FROM projection_revisions WHERE revision_id=?", (second.revision_id,)
                )
            rebuilt = engine.rebuild(published_at=LATER)
            assert rebuilt.revision_id == second.revision_id and rebuilt.data == second.data
            assert engine.revision(first.revision_id) == first
            recovery = RecoveryStore(root / "recovery")
            backup = recovery.backup(store)
            recovery.restore(backup, root / "restored.sqlite")
            with RawStore(root / "restored.sqlite") as restored:
                restored_engine = ProjectionEngine(restored)
                assert restored_engine.revision().data == second.data
                assert restored_engine.status()["state"] == "current"
            expected = second.revision_id
            revisions = [
                {
                    "revision_id": r.revision_id,
                    "input_hash": r.input_hash,
                    "watermark": r.watermark,
                    "artifact": name,
                }
                for r, name in [(first, "recovered.json"), (second, "rerunning.json")]
            ]
            for revision, name in [(first, "recovered.json"), (second, "rerunning.json")]:
                (root / name).write_text(
                    json.dumps(asdict(revision), ensure_ascii=False, indent=2) + "\n"
                )
                (root / name).chmod(0o600)
            report = {
                "task": "SLD-14",
                "matrices": ["V05", "V07", "V08", "V09"],
                "handoff_sha256": HANDOFF_SHA256,
                "reducer_version": REDUCER_VERSION,
                "rule_version": RULE_VERSION,
                "frozen_sources": len(sources),
                "counts": asdict(store.counts()),
                "projection_counts": {
                    kind: len(second.data[kind])
                    for kind in ("runs", "instances", "attempts", "jobs")
                },
                "revisions": revisions,
                "before": {
                    "instance_execution": "recovered",
                    "run_lifecycle": "finish_declared",
                    "attempts": 3,
                },
                "after": {
                    "instance_execution": "rerunning",
                    "run_lifecycle": "needs_finalization",
                    "attempts": 4,
                },
                "cache_rebuild_revision": rebuilt.revision_id,
                "backup": backup,
                "restore": "pass",
                "status": engine.status(),
                "business_jobs_executed": 0,
                "database_sha256": hashlib.sha256((root / "facts.sqlite").read_bytes()).hexdigest(),
            }
        with RawStore(root / "shuffled.sqlite") as store:
            engine = ProjectionEngine(store)
            engine.import_documents(documents)
            shuffled = records[:] + records[:]
            random.Random(14).shuffle(shuffled)
            store.ingest(shuffled)
            actual = engine.rebuild(published_at=LATER)
            assert actual.revision_id == expected and actual.data == second.data
            report["shuffled_duplicate_replay"] = {
                "seed": 14,
                "revision_id": actual.revision_id,
                "same_state": True,
            }
        for locator, raw in sources.items():
            assert (root / "shared" / locator).read_bytes() == raw
        report["all_frozen_source_bytes_unchanged"] = True
        if args.report_dir:
            (root / "replay.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n"
            )
            (root / "replay.json").chmod(0o600)
        print(
            json.dumps(
                {
                    "before": report["before"],
                    "after": report["after"],
                    "revision_rebuilt": expected,
                    "shuffle_and_backup_restore": "pass",
                }
            )
        )


if __name__ == "__main__":
    main()
