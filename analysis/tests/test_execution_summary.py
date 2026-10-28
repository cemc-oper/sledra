"""Execution reduction and counts over synthetic v2 facts, without legacy services."""

from __future__ import annotations

import copy
import gc
import json
import os
import subprocess
import sys
import tempfile
import unittest
import uuid
import weakref
from pathlib import Path

from sledra_analysis.execution.codec import (
    ContractError,
    execution_path,
    partition_date,
    validate,
)
from sledra_analysis.execution.reader import Fact, FileReader, ReadIssue, Source
from sledra_analysis.execution.summary import (
    associate_run,
    events_for_target_cycle,
    group_counts,
    iter_summaries,
    summarize,
)

ROOT = Path(__file__).resolve().parents[2]
CASES = ROOT / "spec/execution-v2/cases"
NAMESPACE = {"system": "mcv", "deployment": "synthetic"}
WHEN = "2026-10-27T23:59:59.000000001+08:00"
CYCLE = "2024-07-01T00:00:00+08:00"


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def event(identity, name, attrs, *, when=WHEN, event_id=None):
    return {
        "format_version": "2.0.0",
        "event_id": event_id or str(uuid.uuid4()),
        "name": name,
        "occurred_at": when,
        "execution_id": identity,
        "attributes": attrs,
    }


def context(**changes):
    return {
        "task": "main.forecast",
        "parameters": {"resolution_km": 25, "member": "01"},
        "scope": None,
        "cycle_kind": "single",
        "cycle": CYCLE,
        "run_id": None,
        **changes,
    }


def facts(events, path="synthetic.jsonl"):
    offset = 0
    for number, item in enumerate(events, 1):
        raw = encoded(item) if isinstance(item, dict) else item
        yield Fact("event", validate("event", raw), Source(path, number, offset, len(raw) + 1))
        offset += len(raw) + 1


def execution(*, identity=None, started=True, **changes):
    identity = identity or str(uuid.uuid4())
    records = [event(identity, "execution.context", context(**changes))]
    if started:
        records.insert(
            0, event(identity, "execution.started", {"task": "entry.task", "engine": None})
        )
    return summarize(facts(records))


class SummaryTests(unittest.TestCase):
    def test_frozen_stream_conflict_vectors_and_two_same_name_stages(self):
        for case in json.loads((CASES / "cases.json").read_bytes()):
            if case["kind"] != "stream":
                continue
            with self.subTest(case=case["file"]):
                raw = (CASES / case["file"]).read_bytes()
                result = summarize(facts(raw.splitlines()))
                self.assertEqual(list(result.conflicts), case["conflicts"])
                self.assertEqual(sum(len(e.sources) for e in result.events), len(raw.splitlines()))
                if case["file"] == "lifecycle.jsonl":
                    self.assertEqual(len(result.stages), 2)
                    self.assertEqual([s.name for s in result.stages], ["forecast", "forecast"])
                    self.assertNotEqual(result.stages[0].stage_id, result.stages[1].stage_id)
                    self.assertEqual(result.stages[1].end.attributes["signal"], 15)
                    self.assertEqual(result.duration.declared_ns, 1_000_000_000)
                    self.assertEqual(result.duration.wall_ns, 0)

    def test_exact_retransmission_preserves_locations_and_original_variants(self):
        identity, eid = str(uuid.uuid4()), str(uuid.uuid4())
        item = event(identity, "forecast.progress", {"number": 1, "text": "é"}, event_id=eid)
        raw = encoded(item)
        other = raw.replace(b'"number":1', b'"number":1e0').replace(
            WHEN.encode(), b"2026-10-27T15:59:59.000000001Z"
        )
        result = summarize(facts([raw, other]))
        self.assertEqual(len(result.events), 1)
        self.assertEqual([s.line for s in result.events[0].sources], [1, 2])
        self.assertEqual(result.conflicts, ())
        for value in (b"true", b"9007199254740993", b'"1"'):
            different = raw.replace(b'"number":1', b'"number":' + value)
            result = summarize(facts([raw, other, different]))
            self.assertEqual(len(result.events), 2)
            self.assertEqual(result.conflicts, ("conflicting_event_id",))
        different = raw.replace("é".encode(), "e\u0301".encode())
        self.assertEqual(len(summarize(facts([raw, different])).events), 2)

    def test_context_conflict_has_no_winner_in_any_iteration_order(self):
        identity = str(uuid.uuid4())
        a = event(identity, "execution.context", context())
        same = event(identity, "execution.context", context(cycle="2024-06-30T16:00:00Z"))
        b = event(identity, "execution.context", context(parameters={"resolution_km": 3}))
        self.assertEqual(summarize(facts([a, same])).context.status, "unique")
        for records in ([a, same, b], [b, same, a]):
            result = summarize(facts(records))
            self.assertEqual(result.context.status, "conflicting")
            self.assertIsNone(result.context.attributes)
            self.assertEqual(len(result.context.records), 3)
            grouped = group_counts([result], namespace=NAMESPACE)
            self.assertEqual(grouped.total.visible, 1)
            self.assertEqual(grouped.uncertain.visible, 1)
            self.assertEqual(grouped.groups, ())

    def test_event_id_collision_cannot_choose_a_context_or_end(self):
        identity, eid = str(uuid.uuid4()), str(uuid.uuid4())
        a = event(identity, "execution.context", context(), event_id=eid)
        b = event(identity, "forecast.progress", {}, event_id=eid)
        result = summarize(facts([a, b]))
        self.assertEqual(result.context.status, "conflicting")
        a = event(identity, "execution.ended", {"exit_code": 0}, event_id=eid)
        result = summarize(facts([a, b]))
        self.assertEqual(result.end.status, "conflicting")
        self.assertIsNone(result.end.attributes)

    def test_missing_context_none_multiple_and_start_counts_remain_distinct(self):
        identity = str(uuid.uuid4())
        only_business = summarize(facts([event(identity, "forecast.progress", {})]))
        started = summarize(
            facts(
                [event(str(uuid.uuid4()), "execution.started", {"task": "entry", "engine": None})]
            )
        )
        none = execution(cycle_kind="none", cycle=None)
        multiple = execution(cycle_kind="multiple", cycle=None, started=False)
        grouped = group_counts([only_business, started, none, multiple], namespace=NAMESPACE)
        self.assertEqual(
            (grouped.total.visible, grouped.total.started, grouped.total.missing_start), (4, 2, 2)
        )
        self.assertEqual(grouped.uncertain.visible, 2)
        self.assertEqual({g.key["cycle_kind"] for g in grouped.groups}, {"none", "multiple"})
        self.assertEqual(only_business.context.status, "missing")
        self.assertEqual(started.end.status, "missing")
        self.assertIsNone(started.end.attributes)  # Not inferred running/success.
        self.assertIsNone(summarize([ReadIssue("invalid_json", Source("bad.jsonl", 1))]))

    def test_full_parameter_groups_distinguish_types_absence_and_unicode(self):
        parameters = [
            {"resolution_km": 3},
            {"resolution_km": 25},
            {"resolution_km": "25"},
            {"resolution_km": True},
            {"resolution_km": 1},
            {},
            {"resolution_km": None},
            {"resolution_km": 25, "member": "01"},
            {"label": "é"},
            {"label": "e\u0301"},
        ]
        rows = [execution(parameters=p) for p in parameters]
        # Numeric 25.0 equals 25; ordering of complete parameter keys is immaterial.
        numeric = execution(parameters={"resolution_km": 25.0})
        rows.append(numeric)
        grouped = group_counts(rows, namespace=NAMESPACE)
        self.assertEqual(len(grouped.groups), len(parameters))
        self.assertEqual(grouped.total.started, 11)
        self.assertEqual(sorted(g.counts.started for g in grouped.groups), [1] * 9 + [2])
        a = execution(parameters={"resolution_km": 25, "member": "01"})
        b = execution(parameters={"member": "01", "resolution_km": 25})
        self.assertEqual(len(group_counts([a, b], namespace=NAMESPACE).groups), 1)
        self.assertEqual(len(group_counts(rows, namespace=NAMESPACE, by=("task",)).groups), 1)
        for by in ((), ("task", "task"), ("attempt",)):
            with self.assertRaisesRegex(ContractError, "invalid_grouping"):
                group_counts([], namespace=NAMESPACE, by=by)

    def test_cycle_offsets_scope_and_final_task_define_groups(self):
        a = execution()
        b = execution(cycle="2024-06-30T16:00:00.000000000Z")
        c = execution(cycle="2024-06-30T16:00:00.000000001Z")
        d = execution(scope="other")
        e = execution(task="post.product")
        groups = group_counts([a, b, c, d, e], namespace=NAMESPACE)
        self.assertEqual(len(groups.groups), 4)
        self.assertEqual(groups.groups[0].key["task"], "main.forecast")
        self.assertEqual(groups.groups[0].key["namespace"], NAMESPACE)
        self.assertEqual(groups.groups[0].counts.started, 2)

    def test_parameter_precision_beyond_binary64_is_preserved(self):
        rows = []
        for token in (
            b"9007199254740992",
            b"9007199254740993",
            b"1.0000000000000000001",
            b"1.0000000000000000002",
        ):
            raw = encoded(
                event(
                    str(uuid.uuid4()),
                    "execution.context",
                    context(parameters={"threshold": "TOKEN"}),
                )
            )
            rows.append(summarize(facts([raw.replace(b'"TOKEN"', token)])))
        grouped = group_counts(rows, namespace=NAMESPACE)
        self.assertEqual(len(grouped.groups), 4)
        self.assertEqual(grouped.total.missing_start, 4)

    def test_identical_start_and_end_repetitions_do_not_inflate_execution_count(self):
        identity = str(uuid.uuid4())
        first = event(identity, "execution.started", {"task": "entry", "engine": None})
        ended = event(identity, "execution.ended", {"exit_code": 0})
        result = summarize(facts([first, first, ended, ended]))
        grouped = group_counts([result], namespace=NAMESPACE)
        self.assertEqual((grouped.total.started, grouped.total.visible), (1, 1))
        self.assertEqual(len(result.events), 2)
        self.assertEqual(len(result.end.records[0].sources), 2)
        self.assertEqual(result.end.status, "unique")

    def test_ended_attributes_not_time_choose_result_and_duration(self):
        identity = str(uuid.uuid4())
        first = event(identity, "execution.started", {"task": "entry", "engine": None})
        ended = event(
            identity,
            "execution.ended",
            {"exit_code": 0, "duration_ns": 7},
            when="2026-10-27T15:59:59.000000009Z",
        )
        result = summarize(facts([first, ended]))
        self.assertEqual((result.duration.declared_ns, result.duration.wall_ns), (7, 8))
        self.assertEqual(
            result.end.attributes["exit_code"], 0
        )  # An exit fact, not product delivery.
        second = copy.deepcopy(ended)
        second["event_id"] = str(uuid.uuid4())
        second["occurred_at"] = "2026-10-27T15:59:59.000000010Z"
        result = summarize(facts([first, ended, second]))
        self.assertEqual(result.end.status, "unique")
        self.assertEqual(result.duration.status, "ambiguous_wall_time")
        self.assertEqual(result.duration.declared_ns, 7)
        self.assertIsNone(result.duration.wall_ns)
        for attrs in (
            {"signal": 15},
            {"exit_code": 1},
            {"exit_code": 0},
            {"exit_code": 0, "duration_ns": 8},
        ):
            second["attributes"] = attrs
            result = summarize(facts([first, ended, second]))
            self.assertEqual(result.end.status, "conflicting")
            self.assertIsNone(result.end.attributes)
            self.assertIsNone(result.duration.declared_ns)
            self.assertIn("conflicting_end", result.conflicts)

    def test_missing_endpoints_and_negative_wall_clock_are_not_fabricated(self):
        identity = str(uuid.uuid4())
        first = event(identity, "execution.started", {"task": "entry", "engine": None})
        ended = event(
            identity,
            "execution.ended",
            {"signal": 15, "duration_ns": 20},
            when="2026-10-27T15:59:59Z",
        )
        result = summarize(facts([ended, first]))
        self.assertEqual(result.duration.wall_ns, -1)
        self.assertEqual(result.duration.declared_ns, 20)
        self.assertEqual(result.duration.status, "negative_wall_time")
        for records, status in (([first], "missing_end"), ([ended], "missing_start")):
            result = summarize(facts(records))
            self.assertEqual(result.duration.status, status)
            self.assertIsNone(result.duration.wall_ns)
            self.assertIsNone(result.duration.declared_ns)

    def test_stage_pairing_uses_id_and_retains_orphans_conflicts_and_names(self):
        identity = str(uuid.uuid4())
        one, two, three, four = (str(uuid.uuid4()) for _ in range(4))
        records = [
            event(identity, "stage.ended", {"stage": "forecast", "stage_id": one, "exit_code": 0}),
            event(identity, "stage.started", {"stage": "forecast", "stage_id": two}),
            event(identity, "stage.started", {"stage": "a", "stage_id": three}),
            event(identity, "stage.ended", {"stage": "b", "stage_id": three, "exit_code": 0}),
            event(identity, "stage.started", {"stage": "forecast", "stage_id": four}),
            event(identity, "stage.ended", {"stage": "forecast", "stage_id": four, "exit_code": 0}),
            event(identity, "stage.ended", {"stage": "forecast", "stage_id": four, "signal": 15}),
        ]
        result = summarize(facts(records))
        self.assertEqual(
            [s.duration.status for s in result.stages],
            ["missing_start", "missing_end", "conflicting_stage", "conflicting_stage"],
        )
        self.assertIn("stage_name_mismatch", result.conflicts)
        self.assertIn("conflicting_stage_end", result.conflicts)
        self.assertIsNone(result.stages[2].name)
        self.assertEqual(result.stages[3].end.status, "conflicting")
        # Input order does not pair same-name intervals across their IDs.
        self.assertEqual(len(summarize(facts(list(reversed(records)))).stages), 4)

    def test_target_cycle_selects_only_business_events_without_reassigning_execution(self):
        identity = str(uuid.uuid4())
        target = "2024-07-02T00:00:00+08:00"
        records = [event(identity, "execution.context", context(cycle_kind="multiple", cycle=None))]
        for value in (target, "2024-07-01T16:00:00Z", "20240702", 20240702, None):
            records.append(event(identity, "forecast.input", {"target_cycle": value}))
        result = summarize(facts(records))
        self.assertEqual(len(events_for_target_cycle(result, target)), 2)
        self.assertEqual(result.context.attributes["cycle_kind"], "multiple")
        self.assertIsNone(result.context.attributes["cycle"])
        self.assertEqual(group_counts([result], namespace=NAMESPACE).total.visible, 1)
        with self.assertRaisesRegex(ContractError, "invalid_time"):
            events_for_target_cycle(result, "20240702")

    def test_mixed_execution_or_non_event_input_is_rejected(self):
        with self.assertRaisesRegex(ContractError, "execution_mismatch"):
            summarize(
                facts(
                    [
                        event(str(uuid.uuid4()), "forecast.input", {}),
                        event(str(uuid.uuid4()), "forecast.input", {}),
                    ]
                )
            )
        with self.assertRaisesRegex(ContractError, "unknown_kind"):
            summarize([Fact("run", {}, Source("run.json"))])


class FileSummaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sledra summary synthetic ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "root"
        self.root.mkdir()
        self.config = json.loads((CASES / "root.json").read_bytes())
        (self.root / "sledra.json").write_bytes(encoded(self.config))

    def put_events(self, records, *, raw=None, entry_time=WHEN):
        path = self.root / execution_path(records[0]["execution_id"], entry_time, self.config)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw if raw is not None else b"".join(encoded(r) + b"\n" for r in records))
        return path

    def put_run(self, *, kind="single", cycle=CYCLE, scope="forecast", identity=None):
        run = json.loads((CASES / "run-single.json").read_bytes())
        run.update(run_id=identity or str(uuid.uuid4()), cycle_kind=kind, cycle=cycle, scope=scope)
        run["namespace"] = self.config["namespace"]
        path = (
            self.root
            / "runs"
            / partition_date(run["created_at"], self.config)
            / (run["run_id"] + ".json")
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encoded(run))
        return run, path

    def test_two_run_rounds_retry_alias_and_auxiliary_are_grouped_from_synthetic_facts(self):
        first, _ = self.put_run()
        second, _ = self.put_run()
        summaries = [
            execution(scope="forecast", run_id=first["run_id"]),
            execution(scope="forecast", run_id=first["run_id"]),
            execution(scope="forecast", run_id=second["run_id"]),
            execution(scope="forecast", run_id=None),  # Synthetic independent alias.
            execution(
                scope="forecast",
                run_id=first["run_id"],
                cycle_kind="none",
                cycle=None,
                task="admin.cleanup",
            ),
        ]
        # Retain the actual node/alias attribute without guessing parent identity.
        summaries[3].start.records[0].data["attributes"]["engine"] = {
            "kind": "ecflow",
            "service": "synthetic",
            "node": "/mcv/main/alias0",
            "alias": True,
        }
        with FileReader(self.root) as reader:
            lookup = reader.resolve_runs([first["run_id"], second["run_id"]])
        grouped = group_counts(
            summaries, namespace=self.config["namespace"], runs=lookup, by=("run",)
        )
        self.assertEqual(len(grouped.groups), 3)
        self.assertEqual(sorted(g.counts.started for g in grouped.groups), [1, 1, 3])
        self.assertEqual(grouped.uncertain.visible, 0)
        self.assertIsNone(summaries[4].context.attributes["cycle"])
        by_cycle = group_counts(summaries, namespace=self.config["namespace"], by=("cycle",))
        self.assertEqual(len(by_cycle.groups), 2)
        self.assertEqual(sorted(g.counts.started for g in by_cycle.groups), [1, 4])

    def test_run_scope_cycle_matrix_missing_and_conflict_preserve_execution_visibility(self):
        one, _ = self.put_run()
        none, _ = self.put_run(kind="none", cycle=None)
        many, _ = self.put_run(kind="multiple", cycle=None)
        missing = str(uuid.uuid4())
        ids = [one["run_id"], none["run_id"], many["run_id"], missing]
        with FileReader(self.root) as reader:
            lookup = reader.resolve_runs(ids)
        cases = [
            (one, "single", CYCLE, "forecast", "associated"),
            (one, "single", "2024-06-30T16:00:00Z", "forecast", "associated"),
            (one, "single", "2024-07-02T00:00:00Z", "forecast", "cycle_mismatch"),
            (one, "none", None, "forecast", "associated"),
            (one, "multiple", None, "forecast", "cycle_mismatch"),
            (one, "single", CYCLE, "other", "scope_mismatch"),
            (none, "none", None, "forecast", "associated"),
            (none, "single", CYCLE, "forecast", "cycle_mismatch"),
            (none, "multiple", None, "forecast", "cycle_mismatch"),
            (many, "single", CYCLE, "forecast", "associated"),
            (many, "none", None, "forecast", "associated"),
            (many, "multiple", None, "forecast", "associated"),
        ]
        for run, kind, cycle, scope, expected in cases:
            summary = execution(run_id=run["run_id"], scope=scope, cycle_kind=kind, cycle=cycle)
            self.assertEqual(associate_run(summary, lookup).status, expected)
        summary = execution(run_id=missing, scope="forecast")
        self.assertEqual(associate_run(summary, lookup).status, "missing")
        self.assertEqual(
            group_counts([summary], namespace=NAMESPACE, runs=lookup).uncertain.visible, 1
        )
        self.assertEqual(len(group_counts([summary], namespace=NAMESPACE, by=("cycle",)).groups), 1)
        self.assertEqual(associate_run(summary, None).status, "not_loaded")
        # A namespace mismatch is rejected by the reader, never grouped as linked.
        foreign, path = self.put_run()
        foreign["namespace"] = {"system": "foreign", "deployment": "elsewhere"}
        path.write_bytes(encoded(foreign))
        with FileReader(self.root) as reader:
            lookup = reader.resolve_runs([foreign["run_id"]])
        summary = execution(run_id=foreign["run_id"], scope="forecast")
        self.assertEqual(associate_run(summary, lookup).status, "unavailable")
        self.assertEqual(
            group_counts([summary], namespace=NAMESPACE, runs=lookup).uncertain.visible, 1
        )
        # Same ID, different valid creation dates/content has no selected winner.
        old, _ = self.put_run(identity=one["run_id"])
        old["created_at"] = "2024-07-01T00:00:00+08:00"
        path = self.root / "runs/20240701" / (old["run_id"] + ".json")
        path.parent.mkdir(parents=True)
        path.write_bytes(encoded(old))
        with FileReader(self.root) as reader:
            lookup = reader.resolve_runs([one["run_id"]])
        self.assertEqual(
            associate_run(execution(run_id=one["run_id"], scope="forecast"), lookup).status,
            "conflicting",
        )

    def test_bad_rows_partial_tail_and_window_rebuild_without_query_state(self):
        identity = str(uuid.uuid4())
        records = [
            event(identity, "execution.started", {"task": "entry", "engine": None}),
            event(identity, "execution.context", context()),
        ]
        path = self.put_events(
            records, raw=encoded(records[0]) + b"\n{bad}\n" + encoded(records[1])
        )
        bad = self.root / execution_path(str(uuid.uuid4()), WHEN, self.config)
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_bytes(b"{bad}\n")
        with FileReader(self.root) as reader:
            rows = list(iter_summaries(reader, "2026-10-27", "2026-10-27"))
            result = group_counts(rows, namespace=self.config["namespace"])
        self.assertEqual(result.total.started, 1)
        self.assertEqual(result.uncertain.visible, 1)
        self.assertEqual(
            sorted(i.code for i in result.read_issues),
            ["incomplete_tail", "invalid_json", "invalid_json"],
        )
        path.write_bytes(path.read_bytes() + b"\n")
        before = {
            str(p.relative_to(self.root)): p.read_bytes()
            for p in self.root.rglob("*")
            if p.is_file()
        }
        for _ in range(2):
            with FileReader(self.root) as reader:
                result = group_counts(
                    iter_summaries(reader, "2026-10-27", "2026-10-27"),
                    namespace=reader.config["namespace"],
                )
                empty = group_counts(
                    iter_summaries(reader, "2026-10-28", "2026-10-28"),
                    namespace=reader.config["namespace"],
                )
            self.assertEqual(result.total.visible, 1)
            self.assertEqual(result.uncertain.visible, 0)
            self.assertEqual(empty.total.visible, 0)
            self.assertEqual(len(result.read_issues), 2)
            self.assertEqual(result.groups[0].key["cycle"], CYCLE)
        self.assertEqual(
            before,
            {
                str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob("*")
                if p.is_file()
            },
        )

    def test_duplicate_files_count_once_and_cannot_leave_a_definite_group(self):
        identity = str(uuid.uuid4())
        # Without a start, either file is readable; duplicate physical ownership
        # must not inflate counts or pick the last context as the winner.
        records = [event(identity, "execution.context", context())]
        self.put_events(records)
        self.put_events(records, entry_time="2026-10-28T00:00:00+08:00")
        with FileReader(self.root) as reader:
            result = group_counts(
                iter_summaries(reader, None, None), namespace=reader.config["namespace"]
            )
        self.assertEqual(result.total.visible, 1)
        self.assertEqual(result.total.missing_start, 1)
        self.assertEqual(result.duplicate_execution_ids, (identity,))
        self.assertEqual(result.uncertain.visible, 1)
        self.assertEqual(result.groups, ())

    def test_counts_release_events_as_files_are_consumed(self):
        refs = []

        def rows():
            for _ in range(300):
                result = execution()
                refs.append(weakref.ref(result))
                yield result

        result = group_counts(rows(), namespace=NAMESPACE)
        gc.collect()
        self.assertEqual(result.total.started, 300)
        self.assertTrue(all(ref() is None for ref in refs))

    def test_fresh_process_read_reduce_group_has_no_write_database_or_network(self):
        identity = str(uuid.uuid4())
        self.put_events([event(identity, "execution.context", context())])
        script = r"""
import os,sys
sys.path.insert(0,sys.argv[1])
def audit(event,args):
    if event=="socket.connect" or event.startswith("sqlite3.connect"):
        raise AssertionError(event)
    if event=="open" and args[2] & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND):
        raise AssertionError("unexpected write")
sys.addaudithook(audit)
from sledra_analysis.execution.reader import FileReader
from sledra_analysis.execution.summary import group_counts,iter_summaries
with FileReader(sys.argv[2]) as reader:
    result=group_counts(iter_summaries(reader,None,None),namespace=reader.config["namespace"])
assert result.total.visible==1 and result.total.started==0
assert len(result.groups)==1
assert not any(n=="sqlite3" or n.startswith(("sledra_analysis.ingest","sledra_analysis.store","sledra_analysis.projections","sledra_analysis.metrics")) for n in sys.modules)
"""
        result = subprocess.run(
            [sys.executable, "-I", "-B", "-c", script, str(ROOT / "analysis/src"), str(self.root)],
            cwd=self.temp.name,
            capture_output=True,
            timeout=15,
            env={k: v for k, v in os.environ.items() if k != "PYTHONPATH"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
