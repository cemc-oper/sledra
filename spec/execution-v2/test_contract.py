"""Contract vectors and boundary regressions; all inputs are synthetic or temporary."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from check import (
    MAX_RECORD_BYTES,
    SPEC,
    ContractError,
    comparable,
    decode,
    equivalent,
    execution_path,
    partition_date,
    run_conflicts,
    stream_conflicts,
    timestamp,
    validate,
)

CASES = SPEC / "cases"


def encoded(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class ContractTests(unittest.TestCase):
    def rejected(self, code: str, operation, *args) -> None:
        with self.assertRaises(ContractError) as caught:
            operation(*args)
        self.assertEqual(caught.exception.code, code)

    def progress(self, attributes: dict) -> bytes:
        value = json.loads((CASES / "signal.json").read_bytes())
        value.update(name="forecast.progress", attributes=attributes)
        return encoded(value)

    def test_shared_cases(self) -> None:
        cases = json.loads((CASES / "cases.json").read_bytes())
        for case in cases:
            with self.subTest(case=case["file"]):
                raw = (CASES / case["file"]).read_bytes()
                if case["kind"] == "stream":
                    self.assertEqual(stream_conflicts(raw), case["conflicts"])
                elif case["kind"] == "run-set":
                    self.assertEqual(
                        run_conflicts([encoded(record) for record in json.loads(raw)]),
                        case["conflicts"],
                    )
                elif case["expected"] == "ok":
                    validate(case["kind"], raw)
                else:
                    self.rejected(case["expected"], validate, case["kind"], raw)
        names = {case["file"] for case in cases} | {"cases.json", "comparisons.json", "layout.json"}
        self.assertEqual(names, {path.name for path in CASES.iterdir()})

    def test_exact_numeric_and_unicode_comparison_vectors(self) -> None:
        for vector in json.loads((CASES / "comparisons.json").read_bytes()):
            with self.subTest(vector=vector):
                self.assertEqual(
                    equivalent(decode(vector["left"].encode()), decode(vector["right"].encode())),
                    vector["equal"],
                )
        raw = (CASES / "decimal-and-int64.json").read_bytes()
        attrs = validate("event", raw)["attributes"]
        self.assertEqual(attrs["steps"], 9007199254740993)
        self.assertNotEqual(attrs["steps"], 9007199254740992)
        self.assertEqual(attrs["minimum"], -(2**63))
        self.assertEqual(attrs["maximum"], 2**63 - 1)

    def test_record_byte_boundary_includes_whitespace(self) -> None:
        raw = self.progress({"ok": True})
        validate("event", raw + b" " * (MAX_RECORD_BYTES - len(raw)))
        self.rejected(
            "limit_exceeded", validate, "event", raw + b" " * (MAX_RECORD_BYTES + 1 - len(raw))
        )

    def test_decoded_utf8_string_and_key_boundaries(self) -> None:
        validate("event", self.progress({"text": "é" * 2048}))
        self.rejected("limit_exceeded", validate, "event", self.progress({"text": "é" * 2049}))
        validate("event", self.progress({"é" * 64: True}))
        self.rejected("limit_exceeded", validate, "event", self.progress({"é" * 65: True}))
        self.rejected("invalid_unicode", validate, "event", self.progress({"bad\0key": True}))

    def test_container_depth_and_cardinality_boundaries(self) -> None:
        # Envelope root + attributes + 14 lists = 16 containers.
        value = 0
        for _ in range(14):
            value = [value]
        validate("event", self.progress({"nested": value}))
        self.rejected("limit_exceeded", validate, "event", self.progress({"nested": [value]}))
        for value in (list(range(128)), {f"k{i}": i for i in range(128)}):
            validate("event", self.progress({"items": value}))
        for value in (list(range(129)), {f"k{i}": i for i in range(129)}):
            self.rejected("limit_exceeded", validate, "event", self.progress({"items": value}))
        self.rejected("limit_exceeded", decode, b"[" * 2000 + b"0" + b"]" * 2000)

    def test_scalar_number_lexemes_are_preserved(self) -> None:
        for token in (b"NaN", b"Infinity", b"-Infinity"):
            self.rejected("invalid_number", decode, token)
        for token in (b"1e309", b"1e-999", b"1e999999999999999999999999999999"):
            self.rejected("number_out_of_range", decode, token)
        self.rejected("number_out_of_range", decode, b"9" * 5000)
        # Subnormals are supported if binary64 conversion does not produce zero.
        for token in (b"5e-324", b"1.7976931348623157e308", b"-0.0", b"0e-999"):
            decode(token)
        raw = (CASES / "signal.json").read_bytes()
        for token in (b"true", b"15.0", b"15e0", b"0", b"128"):
            self.rejected(
                "schema_invalid",
                validate,
                "event",
                raw.replace(b'"signal":15', b'"signal":' + token),
            )

    def test_time_precision_offset_and_calendar(self) -> None:
        self.assertEqual(timestamp("2026-10-27T00:00:00+08:00"), timestamp("2026-10-26T16:00:00Z"))
        self.assertNotEqual(
            timestamp("2026-10-27T00:00:00.000000001Z"),
            timestamp("2026-10-27T00:00:00.000000002Z"),
        )
        for value in (
            "2026-10-27T00:00:60Z",
            "2026-10-27T24:00:00Z",
            "2026-10-27T00:00:00+01:60",
            "2026-10-27T00:00:00.1234567890Z",
            "0000-01-01T00:00:00Z",
        ):
            self.rejected("invalid_time", timestamp, value)
        timestamp("2024-02-29T00:00:00Z")

    def test_layout_uses_entry_date_and_exact_uuid_bytes(self) -> None:
        vector = json.loads((CASES / "layout.json").read_bytes())
        self.assertEqual(
            execution_path(vector["execution_id"], vector["entry_time"], vector["root"]),
            vector["path"],
        )
        # The later event would choose another date: writer must retain entry path.
        self.assertNotEqual(
            execution_path(vector["execution_id"], vector["later_event_time"], vector["root"]),
            vector["path"],
        )
        run = validate("run", (CASES / "run-single.json").read_bytes())
        date = partition_date(run["created_at"], vector["root"])
        self.assertEqual(f"runs/{date}/{run['run_id']}.json", vector["run_path"])
        root = copy.deepcopy(vector["root"])
        for zone in ("+14:00", "-14:00", "+00:00", "+05:30"):
            root["partition_timezone"] = zone
            validate("root", encoded(root))
        for zone in ("-00:00", "+15:00", "+08:60"):
            root["partition_timezone"] = zone
            self.rejected("invalid_timezone", validate, "root", encoded(root))

    def test_run_identity_conflicts_do_not_need_registry(self) -> None:
        raw = (CASES / "run-single.json").read_bytes()
        value = json.loads(raw)
        value["cycle"] = "2024-06-30T16:00:00Z"
        value["created_at"] = "2026-10-27T15:59:59Z"
        self.assertEqual(run_conflicts([raw, encoded(value)]), [])
        value["scope"] = "other_scope"
        self.assertEqual(run_conflicts([raw, encoded(value)]), ["conflicting_run_id"])
        self.assertNotEqual(comparable(validate("run", raw), "run")["scope"], value["scope"])

    def test_repeated_stage_names_have_distinct_intervals(self) -> None:
        events = [
            json.loads(line) for line in (CASES / "lifecycle.jsonl").read_bytes().splitlines()
        ]
        starts = [e["attributes"] for e in events if e["name"] == "stage.started"]
        ends = [e["attributes"] for e in events if e["name"] == "stage.ended"]
        self.assertEqual([s["stage"] for s in starts], ["forecast", "forecast"])
        self.assertEqual(len({s["stage_id"] for s in starts}), 2)
        self.assertEqual([s["stage_id"] for s in starts], [e["stage_id"] for e in ends])

    def test_stream_framing_and_foreign_execution(self) -> None:
        raw = (CASES / "unresolved.jsonl").read_bytes()
        self.rejected("incomplete_tail", stream_conflicts, raw[:-1])
        self.rejected("invalid_json", stream_conflicts, raw + b"\n")
        self.rejected("invalid_json", stream_conflicts, raw.replace(b"\n", b"\r\n"))
        self.rejected("invalid_json", validate, "event", self.progress({}) + b"\n ")
        foreign = json.loads((CASES / "signal.json").read_bytes())
        foreign["execution_id"] = "00000000-0000-4000-8000-000000000099"
        self.rejected("execution_mismatch", stream_conflicts, raw + encoded(foreign) + b"\n")

    def test_no_final_context_is_distinct_from_none(self) -> None:
        events = [
            json.loads(line) for line in (CASES / "unresolved.jsonl").read_bytes().splitlines()
        ]
        self.assertFalse(any(e["name"] == "execution.context" for e in events))
        for kind in ("none", "multiple"):
            attrs = validate("event", (CASES / f"context-{kind}.json").read_bytes())["attributes"]
            self.assertEqual(attrs["cycle_kind"], kind)
            self.assertIsNone(attrs["cycle"])

    def test_cli_classifies_and_does_not_echo_values(self) -> None:
        with tempfile.TemporaryDirectory(prefix="sledra-v2-contract-") as temp:
            path = Path(temp) / "input.json"
            path.write_bytes(self.progress({"private": "do-not-echo"}) + b"\0")
            result = subprocess.run(
                [sys.executable, str(SPEC / "check.py"), "event", str(path)],
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, b"")
            self.assertEqual(json.loads(result.stderr), {"ok": False, "code": "invalid_json"})
            self.assertNotIn(b"do-not-echo", result.stderr)
            result = subprocess.run(
                [
                    sys.executable,
                    str(SPEC / "check.py"),
                    "stream",
                    str(CASES / "conflicting-end.jsonl"),
                ],
                capture_output=True,
                check=False,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                json.loads(result.stdout), {"ok": True, "conflicts": ["conflicting_end"]}
            )
            self.assertEqual(list(Path(temp).iterdir()), [path])


if __name__ == "__main__":
    unittest.main(verbosity=2)
