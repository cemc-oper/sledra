"""Immutable timing reports from one committed fact snapshot, with retained inputs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable

from sledra_analysis.metrics.clock import LIMIT, utc_ns
from sledra_analysis.metrics.reducer import METRICS_VERSION, reconstruct_timing
from sledra_analysis.projections.engine import ProjectionEngine, ProjectionError
from sledra_analysis.projections.reducer import (
    REDUCER_VERSION,
    RULE_VERSION,
    event_ref,
    reconstruct,
)
from sledra_analysis.store.canonical import canonical_bytes, canonical_hash, parse_json
from sledra_analysis.store.raw import RawStore, _timestamp


@dataclass(frozen=True)
class TimingReport:
    report_id: str
    input_hash: str
    report_as_of: str
    published_at: str
    watermark: dict
    data: dict


def validate_accounting(record):
    """Versioned analysis input, not a new recorder event or scheduler integration."""
    required = {
        "version",
        "adapter_version",
        "run_id",
        "job_ref_id",
        "submission_instance",
        "interval_id",
        "queue_basis",
        "includes_hold",
        "start",
        "end",
        "because",
    }
    if not isinstance(record, dict) or set(record) != required:
        raise ValueError("invalid accounting fields")
    if (
        record["version"] != "1"
        or record["queue_basis"] not in ("accepted", "eligible")
        or type(record["includes_hold"]) is not bool
    ):
        raise ValueError("unsupported accounting version, queue basis or hold policy")
    for key in ("adapter_version", "run_id", "job_ref_id", "submission_instance", "interval_id"):
        if not isinstance(record[key], str) or not record[key]:
            raise ValueError("accounting identity must be nonempty")
    if (
        not isinstance(record["because"], list)
        or not record["because"]
        or any(not isinstance(r, str) or not r for r in record["because"])
    ):
        raise ValueError("accounting requires raw event references")
    for key in ("start", "end"):
        point = record[key]
        if (
            not isinstance(point, dict)
            or set(point) != {"occurred_at", "time_quality"}
            or not _timestamp(point["occurred_at"])
        ):
            raise ValueError("accounting endpoints need RFC3339 timestamps")
        q = point["time_quality"]
        if (
            not isinstance(q, dict)
            or q.get("basis") not in ("producer_clock", "source_log", "unknown")
            or set(q) - {"basis", "reason", "precision_ns", "clock_error_bound_ns"}
        ):
            raise ValueError("invalid accounting clock quality")
        for field in ("precision_ns", "clock_error_bound_ns"):
            if field in q and (type(q[field]) is not int or not 0 <= q[field] <= LIMIT):
                raise ValueError("invalid accounting clock bound")
    return record


class MetricsEngine:
    """Explicit bounded publication; no business execution, clocks, or background jobs."""

    def __init__(self, store: RawStore, **budgets):
        self.projections = ProjectionEngine(store, **budgets)
        self.store = store
        db = store._db()
        with store.transaction():
            db.execute(
                "CREATE TABLE IF NOT EXISTS timing_metadata(version INTEGER PRIMARY KEY CHECK(version=1)) STRICT"
            )
            versions = db.execute("SELECT version FROM timing_metadata").fetchall()
            if versions and versions != [(1,)]:
                raise ProjectionError("unsupported timing schema")
            db.execute("INSERT OR IGNORE INTO timing_metadata VALUES(1)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS timing_reports(report_id TEXT PRIMARY KEY, version TEXT NOT NULL, input_hash TEXT NOT NULL, input_bytes BLOB NOT NULL, report_as_of TEXT NOT NULL, published_at TEXT NOT NULL, watermark TEXT NOT NULL, data_bytes BLOB NOT NULL) STRICT"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS timing_head(version TEXT PRIMARY KEY, report_id TEXT NOT NULL REFERENCES timing_reports(report_id), watermark TEXT NOT NULL) STRICT"
            )

    def _stage(self, name):
        """Internal fault injection seam; all pre-commit failures roll back together."""

    def publish(
        self,
        *,
        report_as_of: str,
        published_at: str,
        accounting: Iterable[dict] = (),
        cycle_window: tuple[str, str] | None = None,
        engine_mapping: dict | None = None,
    ) -> TimingReport:
        utc_ns(report_as_of)
        utc_ns(published_at)
        if cycle_window is not None and (
            len(cycle_window) != 2 or utc_ns(cycle_window[0]) >= utc_ns(cycle_window[1])
        ):
            raise ValueError("cycle window must be a nonempty half-open UTC range")
        if engine_mapping is not None:
            if (
                not isinstance(engine_mapping, dict)
                or set(engine_mapping) != {"version", "submitted", "active"}
                or not isinstance(engine_mapping["version"], str)
                or not engine_mapping["version"]
            ):
                raise ValueError("engine mapping needs a version and explicit state lists")
            for key in ("submitted", "active"):
                if (
                    not isinstance(engine_mapping[key], list)
                    or not engine_mapping[key]
                    or any(not isinstance(v, str) or not v for v in engine_mapping[key])
                ):
                    raise ValueError("invalid engine state mapping")
            if set(engine_mapping["submitted"]) & set(engine_mapping["active"]):
                raise ValueError("engine state mapping must be disjoint")
            engine_mapping = parse_json(canonical_bytes(engine_mapping))
        records, total = {}, 0
        for index, record in enumerate(accounting):
            if index >= self.projections.max_documents:
                raise ProjectionError("accounting input count exceeds budget")
            encoded = canonical_bytes(validate_accounting(record))
            total += len(encoded)
            if (
                len(encoded) > self.store.max_record_bytes
                or total > self.projections.max_input_bytes
            ):
                raise ProjectionError("accounting input bytes exceed budget")
            # An adapter must revise a conflicting interval explicitly; never pick
            # the first or last record with the same identity.
            key = (record["run_id"], record["job_ref_id"], record["interval_id"])
            if key in records and records[key] != encoded:
                raise ProjectionError("conflicting accounting interval identity")
            records[key] = encoded
        records = [parse_json(records[k]) for k in sorted(records)]
        with self.store.transaction():
            watermark = self.projections._watermark()
            facts, documents, health, rejections = self.projections._inputs()
            by_ref = {event_ref(f): f for f in facts}
            for record in records:
                for ref in record["because"]:
                    fact = by_ref.get(ref)
                    if (
                        not fact
                        or fact["status"] != "known"
                        or fact["event"]["association"].get("run_id") != record["run_id"]
                        or fact["event"]["association"].get("job_ref_id") != record["job_ref_id"]
                    ):
                        raise ProjectionError(
                            "accounting evidence must resolve to this Job's raw facts"
                        )
            material = {
                "facts": facts,
                "documents": documents,
                "health": health,
                "document_rejections": rejections,
                "accounting": records,
                "engine_mapping": engine_mapping,
                "contracts": self.projections.contract_hashes,
                "validator": self.projections.validator.version,
                "state_reducer": REDUCER_VERSION,
                "state_rules": RULE_VERSION,
                "metrics": METRICS_VERSION,
                "report_as_of": report_as_of,
                "cycle_window": list(cycle_window) if cycle_window else None,
            }
            input_bytes = canonical_bytes(material)
            if len(input_bytes) > self.projections.max_input_bytes:
                raise ProjectionError("timing input snapshot exceeds byte budget")
            input_hash = hashlib.sha256(input_bytes).hexdigest()
            report_id = "tim_" + canonical_hash([METRICS_VERSION, input_hash])
            self._stage("after_input_snapshot")
            state = reconstruct(facts, documents, health)
            data = reconstruct_timing(
                facts,
                documents,
                state,
                report_as_of=report_as_of,
                accounting=records,
                engine_mapping=engine_mapping,
                cycle_window=list(cycle_window) if cycle_window else None,
            )
            data["document_rejections"] = rejections
            data["versions"] = {
                "metrics": METRICS_VERSION,
                "state_reducer": REDUCER_VERSION,
                "state_rules": RULE_VERSION,
            }
            encoded = canonical_bytes(data)
            if len(encoded) > self.projections.max_output_bytes:
                raise ProjectionError("timing output exceeds byte budget")
            db = self.store._db()
            db.execute(
                "INSERT INTO timing_reports VALUES(?,?,?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (
                    report_id,
                    METRICS_VERSION,
                    input_hash,
                    input_bytes,
                    report_as_of,
                    published_at,
                    json.dumps(watermark, sort_keys=True),
                    encoded,
                ),
            )
            if db.execute(
                "SELECT input_bytes,data_bytes FROM timing_reports WHERE report_id=?", (report_id,)
            ).fetchone() != (input_bytes, encoded):
                raise ProjectionError("timing report collision or nondeterminism")
            self._stage("after_report")
            db.execute(
                "INSERT INTO timing_head VALUES(?,?,?) ON CONFLICT(version) DO UPDATE SET report_id=excluded.report_id,watermark=excluded.watermark",
                (METRICS_VERSION, report_id, json.dumps(watermark, sort_keys=True)),
            )
            self._stage("after_head")
        self._stage("after_commit")
        return self.report(report_id)

    def report(self, report_id: str | None = None) -> TimingReport | None:
        db = self.store._db()
        if report_id is None:
            head = db.execute(
                "SELECT report_id FROM timing_head WHERE version=?", (METRICS_VERSION,)
            ).fetchone()
            if head is None:
                return None
            report_id = head[0]
        row = db.execute(
            "SELECT report_id,input_hash,report_as_of,published_at,watermark,data_bytes FROM timing_reports WHERE report_id=?",
            (report_id,),
        ).fetchone()
        return TimingReport(*row[:4], json.loads(row[4]), parse_json(row[5])) if row else None

    def inputs(self, report_id: str) -> dict:
        row = (
            self.store._db()
            .execute(
                "SELECT input_bytes,input_hash FROM timing_reports WHERE report_id=?", (report_id,)
            )
            .fetchone()
        )
        if row is None:
            raise KeyError(report_id)
        if hashlib.sha256(row[0]).hexdigest() != row[1]:
            raise ProjectionError("timing snapshot digest mismatch")
        return parse_json(row[0])

    def status(self) -> dict:
        if self.store._active:
            raise ProjectionError("timing status requires committed state")
        current = self.projections._watermark()
        head = (
            self.store._db()
            .execute(
                "SELECT report_id,watermark FROM timing_head WHERE version=?", (METRICS_VERSION,)
            )
            .fetchone()
        )
        consumed = json.loads(head[1]) if head else None
        return {
            "report_id": head[0] if head else None,
            "state": "current"
            if consumed
            and consumed["lineage"] == current["lineage"]
            and consumed["queue_id"] >= current["queue_id"]
            else "pending",
            "source_watermark": current,
            "report_watermark": consumed,
        }
