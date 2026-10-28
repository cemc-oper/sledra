"""Execution v2 production decoder against the frozen P0 shared inputs."""

import json
import unittest
from decimal import Decimal
from pathlib import Path

from sledra_analysis.execution.codec import (
    MAX_RECORD_BYTES,
    ContractError,
    comparable,
    decode,
    equivalent,
    execution_path,
    partition_date,
    timestamp,
    validate,
)

CASES = Path(__file__).resolve().parents[2] / "spec/execution-v2/cases"


class ExecutionCodecTests(unittest.TestCase):
    def rejected(self, code, function, *args):
        with self.assertRaises(ContractError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def test_shared_53_record_cases(self):
        cases = json.loads((CASES / "cases.json").read_bytes())
        self.assertEqual(len(cases), 53)
        for case in cases:
            with self.subTest(case=case["file"]):
                raw = (CASES / case["file"]).read_bytes()
                if case["kind"] == "stream":
                    # Conflict/deduplication reduction belongs to P2-02; every
                    # source fact remains valid and is deliberately retained.
                    self.assertTrue(raw.endswith(b"\n"))
                    records = [validate("event", line) for line in raw.splitlines()]
                    self.assertEqual(len({r["execution_id"] for r in records}), 1)
                elif case["kind"] == "run-set":
                    for record in json.loads(raw):
                        validate("run", json.dumps(record).encode())
                elif case["expected"] == "ok":
                    validate(case["kind"], raw)
                else:
                    self.rejected(case["expected"], validate, case["kind"], raw)

    def test_exact_numbers_booleans_unicode_and_time_comparison(self):
        vectors = json.loads((CASES / "comparisons.json").read_bytes())
        self.assertEqual(len(vectors), 9)
        for vector in vectors:
            self.assertEqual(
                equivalent(decode(vector["left"].encode()), decode(vector["right"].encode())),
                vector["equal"],
            )
        attrs = validate("event", (CASES / "decimal-and-int64.json").read_bytes())["attributes"]
        self.assertEqual(attrs["steps"], 9007199254740993)
        self.assertEqual(attrs["minimum"], -(2**63))
        self.assertEqual(attrs["maximum"], 2**63 - 1)
        self.assertIsInstance(decode(b"0.10000000000000001"), Decimal)
        self.assertNotEqual(decode(b"0.10000000000000001"), decode(b"0.1"))
        runs = [
            validate("run", json.dumps(r).encode())
            for r in json.loads((CASES / "same-run.json").read_bytes())
        ]
        self.assertTrue(equivalent(comparable(runs[0], "run"), comparable(runs[1], "run")))
        self.assertEqual(
            timestamp("2026-10-27T08:00:00.000000001+08:00"),
            timestamp("2026-10-27T00:00:00.000000001Z"),
        )
        self.assertNotEqual(
            timestamp("2026-10-27T00:00:00.000000001Z"), timestamp("2026-10-27T00:00:00.000000002Z")
        )

    def test_framing_integer_lexemes_and_limits(self):
        raw = (CASES / "signal.json").read_bytes()
        for number in (b"15.0", b"15e0", b"true"):
            self.rejected(
                "schema_invalid",
                validate,
                "event",
                raw.replace(b'"signal":15', b'"signal":' + number),
            )
        root = (CASES / "root.json").read_bytes()
        self.rejected(
            "schema_invalid",
            validate,
            "root",
            root.replace(b'"layout_version":2', b'"layout_version":2.0'),
        )
        validate("event", raw + b" " * (MAX_RECORD_BYTES - len(raw)))
        self.rejected(
            "limit_exceeded", validate, "event", raw + b" " * (MAX_RECORD_BYTES + 1 - len(raw))
        )
        self.rejected("invalid_json", validate, "event", raw + b"\n")
        validate("root", root + b"\r\n")
        self.rejected("unknown_kind", validate, "stream", raw)
        self.rejected("limit_exceeded", decode, b"[" * 2000 + b"0" + b"]" * 2000)
        self.rejected("number_out_of_range", decode, b"1e-999")
        self.rejected("number_out_of_range", decode, b"1e309")
        self.rejected("invalid_unicode", decode, b'"\\ud800"')
        for token in (b"5e-324", b"1.7976931348623157e308", b"0e-999"):
            decode(token)
        self.assertEqual(decode(b"0e" + b"9" * 40), Decimal(0))
        self.assertTrue(decode(b"-0e-" + b"9" * 40).is_signed())
        self.rejected("number_out_of_range", decode, b"1e" + b"9" * 40)
        for key, value, expected in [
            ("é" * 64, "é" * 2048, None),
            ("é" * 65, "ok", "limit_exceeded"),
            ("ok", "é" * 2049, "limit_exceeded"),
            ("ok", "bad\0", "invalid_unicode"),
        ]:
            record = json.loads(raw)
            record.update(name="example.progress", attributes={key: value})
            encoded = json.dumps(record, ensure_ascii=False).encode()
            if expected:
                self.rejected(expected, validate, "event", encoded)
            else:
                validate("event", encoded)

    def test_layout_and_known_time_bounds(self):
        vector = json.loads((CASES / "layout.json").read_bytes())
        self.assertEqual(
            execution_path(vector["execution_id"], vector["entry_time"], vector["root"]),
            vector["path"],
        )
        self.assertEqual(
            "runs/"
            + partition_date("2026-10-27T23:59:59+08:00", vector["root"])
            + "/00000000-0000-4000-8000-000000000002.json",
            vector["run_path"],
        )
        self.rejected(
            "schema_invalid", execution_path, "not-an-id", vector["entry_time"], vector["root"]
        )
        self.rejected("invalid_time", partition_date, "0001-01-01T00:00:00+23:00", vector["root"])
        for value in (
            "2026-10-27T00:00:60Z",
            "2026-10-27T24:00:00Z",
            "2026-10-27T00:00:00-00:00",
            "0000-01-01T00:00:00Z",
            "2026-10-27T00:00:00.1234567890Z",
        ):
            self.rejected("invalid_time", timestamp, value)

    def test_depth_and_container_cardinality_match_the_shared_contract(self):
        event = json.loads((CASES / "signal.json").read_bytes())
        event["name"] = "example.progress"
        nested = 0
        for _ in range(14):
            nested = [nested]
        for value, accepted in [
            (nested, True),
            ([nested], False),
            (list(range(128)), True),
            (list(range(129)), False),
            ({f"k{i}": i for i in range(128)}, True),
            ({f"k{i}": i for i in range(129)}, False),
        ]:
            event["attributes"] = {"nested": value}
            raw = json.dumps(event).encode()
            if accepted:
                validate("event", raw)
            else:
                self.rejected("limit_exceeded", validate, "event", raw)


if __name__ == "__main__":
    unittest.main(verbosity=2)
