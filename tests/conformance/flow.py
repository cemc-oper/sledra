"""Private SLD-23 pipeline fixture; never consumes operational paths or runs jobs."""

from __future__ import annotations

import hashlib
import json
import os
import random
import signal
from dataclasses import asdict
from pathlib import Path

from sledra_analysis.ingest.transaction import AtomicIngestor
from sledra_analysis.metrics.engine import MetricsEngine
from sledra_analysis.projections.documents import DocumentRecord
from sledra_analysis.projections.engine import ProjectionEngine
from sledra_analysis.store.canonical import canonical_hash, parse_json
from sledra_analysis.store.raw import RawStore
from sledra_analysis.store.recovery import RecoveryStore

ROOT = Path(__file__).resolve().parents[2]
HANDOFF = ROOT / "tests/m1/results/handoff.json"
HANDOFF_HASH = "5c6c2bf40af052eedaf0edeb4ea02119f6d899824e086c8e26e210a436e282ef"
STAMP = "2026-10-15T00:00:00Z"
CUTS = (
    "ingest:after_records",
    "ingest:after_checkpoint",
    "ingest:before_commit",
    "ingest:after_commit",
    "projection:after_input_snapshot",
    "projection:after_revision",
    "projection:after_work_cursor",
    "projection:before_projection_commit",
    "projection:after_projection_commit",
    "metrics:after_input_snapshot",
    "metrics:after_report",
    "metrics:after_head",
    "metrics:after_commit",
    "recovery:after_raw_archive",
    "recovery:after_archive_commit",
    "recovery:after_snapshot",
    "recovery:after_backup_manifest",
    "recovery:before_ack",
    "recovery:after_ack",
)


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n")
    path.chmod(0o600)


def prepare(root):
    root.mkdir(mode=0o700, parents=True, exist_ok=False)
    shared = root / "shared"
    shared.mkdir(mode=0o700)
    raw = HANDOFF.read_bytes()
    if hashlib.sha256(raw).hexdigest() != HANDOFF_HASH:
        raise ValueError("frozen handoff mismatch")
    inputs, expected, segments, registry, definitions = [], {}, [], [], []
    for item in json.loads(raw)["files"]:
        content = item["text"].encode()
        if hashlib.sha256(content).hexdigest() != item["sha256"]:
            raise ValueError("frozen input digest mismatch")
        # The historical open writers are absent from this private fixture. Only
        # the copied locator is sealed; original frozen bytes are never edited.
        locator = item["path"].replace(".open.jsonl", ".sealed.jsonl")
        path = shared / locator
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        inputs.append(
            {"original_locator": item["path"], "locator": locator, "sha256": item["sha256"]}
        )
        if locator.endswith(".jsonl"):
            segments.append(locator)
            offset = 0
            for index, line in enumerate(content.splitlines(keepends=True)):
                if index:
                    event = parse_json(line)
                    expected.setdefault(event["event_id"] + "@" + canonical_hash(event), []).append(
                        {
                            "locator": locator,
                            "byte_start": offset,
                            "byte_end": offset + len(line),
                            "pointer": "",
                        }
                    )
                offset += len(line)
        else:
            value = parse_json(content)
            if "outbox_event" in value:
                event = value["outbox_event"]
                registry.append(locator)
                expected.setdefault(event["event_id"] + "@" + canonical_hash(event), []).append(
                    {
                        "locator": locator,
                        "byte_start": 0,
                        "byte_end": len(content),
                        "pointer": "/outbox_event",
                    }
                )
            elif value.get("kind") == "definition_manifest":
                definitions.append(locator)
    manifest = {
        "kind": "sledra_conformance_fixture",
        "version": 1,
        "handoff_sha256": HANDOFF_HASH,
        "inputs": inputs,
        "expected_versions": expected,
        "segments": segments,
        "registry": registry,
        "definitions": definitions,
        "seed": 23,
        "source_root_id": "sld-23-private",
    }
    save(root / "inputs.json", manifest)
    return manifest


def summary(store):
    projections, metrics = ProjectionEngine(store), MetricsEngine(store)
    state, timing = projections.revision(), metrics.report()
    return {
        "versions": sorted(
            e.event_id + "@" + v.canonical_hash for e in store.events() for v in e.versions
        ),
        "counts": asdict(store.counts()),
        "quarantine_count": len(store.quarantines()),
        "projection_id": state.revision_id if state else None,
        "projection_data": state.data if state else None,
        "timing_id": timing.report_id if timing else None,
        "timing_data": timing.data if timing else None,
        "projection_status": projections.status(),
        "timing_status": metrics.status(),
        "checkpoint_offsets": {
            c.key.segment_uuid: c.committed_offset for c in store.checkpoints("sld-23-private")
        },
    }


def pipeline(root, *, cut=None, seed=None, duplicate=False):
    root = Path(root)
    manifest = json.loads((root / "inputs.json").read_bytes())
    if (
        manifest["kind"] != "sledra_conformance_fixture"
        or manifest["handoff_sha256"] != HANDOFF_HASH
    ):
        raise ValueError("not a private conformance fixture")

    def seam(component):
        def stage(name):
            if component + ":" + name == cut:
                save(
                    root / "killed-at.json",
                    {"component": component, "stage": name, "input_manifest": "inputs.json"},
                )
                os.kill(os.getpid(), signal.SIGKILL)

        return stage

    with RawStore(root / "facts.sqlite") as store:
        projection, metrics = ProjectionEngine(store), MetricsEngine(store)
        projection._stage, metrics._stage = seam("projection"), seam("metrics")
        projection.import_documents(
            [
                DocumentRecord((root / "shared" / name).read_bytes(), name, STAMP)
                for name in manifest["definitions"]
            ]
        )
        ingest = AtomicIngestor(store, root / "shared", "sld-23-private", chunk_size=23)
        ingest._stage = seam("ingest")
        order = manifest["segments"] + manifest["registry"]
        if duplicate:
            order += order
        if seed is not None:
            random.Random(seed).shuffle(order)
        for name in order:
            if name.endswith(".jsonl"):
                ingest.scan(name, collected_at=STAMP)
            else:
                value = parse_json((root / "shared" / name).read_bytes())
                ingest.registry(
                    name, source_id=value["outbox_event"]["source"]["source_id"], collected_at=STAMP
                )
        projection.rebuild(published_at=STAMP)
        metrics.publish(report_as_of=STAMP, published_at=STAMP)
        recovery = RecoveryStore(root / "recovery")
        recovery._stage = seam("recovery")
        receipts = [
            recovery.acknowledge(
                store, root / "shared", "sld-23-private", name, acknowledged_at=STAMP
            )
            for name in manifest["segments"]
        ]
        # Earlier receipts are per-source snapshots. A final snapshot covers all
        # receipts and all derived heads before any private deletion simulation.
        backup = recovery.backup(store)
        data = summary(store)
        data.update(backup=backup, receipts=receipts)
        save(root / "result.json", data)
        return data


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("--cut", choices=CUTS)
    args = parser.parse_args()
    pipeline(args.root, cut=args.cut)
