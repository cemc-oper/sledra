"""Import a completed synthetic invocation with the public offline Python API."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from sledra_analysis.ingest.transaction import AtomicIngestor
from sledra_analysis.projections.documents import DocumentRecord
from sledra_analysis.projections.engine import ProjectionEngine
from sledra_analysis.store.raw import RawStore


def replay(workspace: Path, database: Path) -> dict:
    root = workspace / "sledra"
    stamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    documents = [
        *sorted((root / "documents").rglob("*.json")),
        *sorted((root / "invocations").rglob("contexts/*.json")),
        *sorted((root / "invocations").rglob("contexts/*/*.json")),
        *sorted((root / "mock-jobs").rglob("*.json")),
    ]
    if not documents:
        raise ValueError("no recorder documents to replay")
    with RawStore(database) as store:
        projection = ProjectionEngine(store)
        projection.import_documents(
            DocumentRecord(path.read_bytes(), str(path.relative_to(workspace)), stamp)
            for path in documents
        )
        ingest = AtomicIngestor(store, root, "sld18-local-replay")
        for path in sorted((root / "registry").rglob("*.json")) + sorted(
            (root / "finish").rglob("*.json")
        ):
            value = json.loads(path.read_text(encoding="utf-8"))
            ingest.registry(
                str(path.relative_to(root)),
                source_id=value["outbox_event"]["source"]["source_id"],
                collected_at=stamp,
            )
        for path in sorted((root / "spool").rglob("*.jsonl")):
            ingest.scan(str(path.relative_to(root)), collected_at=stamp)
        return projection.rebuild(published_at=stamp).data


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", required=True, type=Path)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--summary-out", required=True, type=Path)
    args = parser.parse_args()
    data = replay(args.workspace, args.database)
    args.summary_out.parent.mkdir(parents=True, exist_ok=True)
    args.summary_out.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    main()
