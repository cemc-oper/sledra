"""P0 schema/fixture/interface consistency, without implementing a runtime codec."""

from __future__ import annotations

import json
import unittest

from jsonschema import Draft202012Validator

import test_interfaces
from check import SPEC, ContractError, decode, stream_conflicts, validate


def encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


class ConsistencyTests(unittest.TestCase):
    def test_ascii_identifiers_reject_trailing_line_breaks(self) -> None:
        # '$' alone can match before a final LF in Python's schema regex engine.
        cases = (
            ("event", "signal.json", ("execution_id",)),
            ("event", "signal.json", ("event_id",)),
            ("event", "signal.json", ("name",)),
            ("event", "context-none.json", ("attributes", "scope")),
            ("run", "run-single.json", ("run_id",)),
            ("run", "run-single.json", ("creator_execution_id",)),
            ("run", "run-single.json", ("scope",)),
            ("root", "root.json", ("namespace", "system")),
            ("root", "root.json", ("namespace", "deployment")),
            ("root", "root.json", ("partition_timezone",)),
        )
        for kind, name, path in cases:
            for suffix in ("\n", "\r", "\r\n"):
                with self.subTest(kind=kind, path=path, suffix=repr(suffix)):
                    record = json.loads((SPEC / "cases" / name).read_bytes())
                    parent = record
                    for key in path[:-1]:
                        parent = parent[key]
                    if path == ("name",):
                        parent["name"] = "forecast.progress"
                        parent["attributes"] = {}
                    parent[path[-1]] = (parent[path[-1]] or "forecast") + suffix
                    with self.assertRaises(ContractError) as caught:
                        validate(kind, encoded(record))
                    self.assertEqual(caught.exception.code, "schema_invalid")
        # Free business text still permits escaped line breaks.
        record = json.loads((SPEC / "cases/signal.json").read_bytes())
        record.update(name="forecast.progress", attributes={"message": "first\nsecond"})
        validate("event", encoded(record))
        record = json.loads((SPEC / "cases/context-none.json").read_bytes())
        record["attributes"]["parameters"] = {"resolution_km\n": 25}
        with self.assertRaises(ContractError):
            validate("event", encoded(record))

    def test_schema_top_level_and_selected_entry_agree_on_shared_records(self) -> None:
        schema = json.loads((SPEC / "contract.schema.json").read_bytes())
        Draft202012Validator.check_schema(schema)
        union = Draft202012Validator(schema)
        for case in json.loads((SPEC / "cases/cases.json").read_bytes()):
            if case["expected"] != "ok":
                continue
            raw = (SPEC / "cases" / case["file"]).read_bytes()
            if case["kind"] == "stream":
                records = [decode(line) for line in raw.splitlines()]
            elif case["kind"] == "run-set":
                records = decode(raw)
            else:
                records = [decode(raw)]
            for record in records:
                with self.subTest(case=case["file"]):
                    self.assertTrue(union.is_valid(record))

    def test_schema_references_are_local_and_known(self) -> None:
        schema = json.loads((SPEC / "contract.schema.json").read_bytes())

        def visit(value: object) -> None:
            if isinstance(value, dict):
                if "$ref" in value:
                    target = value["$ref"]
                    self.assertTrue(target.startswith("#/$defs/"), target)
                    self.assertIn(target.removeprefix("#/$defs/"), schema["$defs"])
                for item in value.values():
                    visit(item)
            elif isinstance(value, list):
                for item in value:
                    visit(item)

        visit(schema)

    def test_interface_example_arguments_match_valid_shared_events(self) -> None:
        # Compare fixed public argv with a synthetic format vector, not an encoder.
        probe = test_interfaces.InterfaceExamples()
        probe.setUp()
        self.addCleanup(probe.doCleanups)
        observed = probe.invoke("execution-v2")
        self.assertEqual(observed["rc"], 0)
        raw = (SPEC / "cases/interface-example.jsonl").read_bytes()
        self.assertEqual(stream_conflicts(raw), [])
        events = [decode(line) for line in raw.splitlines()]
        start, context, stage_start, progress, stage_end, ended = events
        attrs = context["attributes"]
        self.assertIsNone(attrs["run_id"])
        self.assertIsNone(attrs["scope"])
        self.assertIsNone(start["attributes"]["engine"])
        self.assertEqual(attrs["task"], start["attributes"]["task"])
        self.assertEqual(
            stage_start["attributes"],
            {
                "stage": stage_end["attributes"]["stage"],
                "stage_id": stage_end["attributes"]["stage_id"],
            },
        )
        expected = [
            ["sledra_execution_begin", "--task", start["attributes"]["task"]],
            [
                "sledra_execution_context",
                "--task",
                attrs["task"],
                "--cycle-kind",
                attrs["cycle_kind"],
                "--cycle",
                attrs["cycle"],
                "--int",
                "resolution_km",
                str(attrs["parameters"]["resolution_km"]),
                "--string",
                "member",
                attrs["parameters"]["member"],
            ],
            ["sledra_stage_begin", stage_start["attributes"]["stage"]],
            [
                "sledra_execution_event",
                progress["name"],
                "--float",
                "fraction",
                str(progress["attributes"]["fraction"]),
                "--string",
                "message",
                progress["attributes"]["message"],
            ],
            ["sledra_stage_end", "--exit-code", str(stage_end["attributes"]["exit_code"])],
            ["sledra_execution_end", "--exit-code", str(ended["attributes"]["exit_code"])],
        ]
        # Tail and EXIT handler request the same fact; actual dedup is P1's job.
        self.assertEqual(observed["hooks"], expected + [expected[-1]])

    def test_public_integer_fields_require_integer_lexemes(self) -> None:
        for kind, filename, original in (
            ("root", "root.json", b'"layout_version":2'),
            ("event", "signal.json", b'"signal":15'),
        ):
            raw = (SPEC / "cases" / filename).read_bytes()
            for suffix in (b".0", b"e0"):
                with self.subTest(field=original, suffix=suffix):
                    with self.assertRaises(ContractError):
                        validate(kind, raw.replace(original, original + suffix))
        record = json.loads((SPEC / "cases/signal.json").read_bytes())
        record["attributes"] = {"exit_code": 0, "duration_ns": 1}
        raw = encoded(record)
        for field in (b'"exit_code":0', b'"duration_ns":1'):
            with self.subTest(field=field):
                with self.assertRaises(ContractError):
                    validate("event", raw.replace(field, field + b".0"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
