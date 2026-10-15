"""Regression gate must refuse missing, skipped, failed or duplicated evidence."""

from __future__ import annotations

import io
import json
import unittest

from matrix import REQUIREMENTS, evaluate
from evidence import EvidenceResult, go_cases


class GateTests(unittest.TestCase):
    def passing_cases(self):
        return [
            {"test": identity, "status": "pass"}
            for identity in sorted({t for _, tests, _ in REQUIREMENTS.values() for t in tests})
        ]

    def test_core_success_never_claims_full_matrix_or_pending_extensions(self):
        result = evaluate(self.passing_cases())
        self.assertTrue(result["core_pass"])
        self.assertFalse(result["full_matrix_pass"])
        self.assertEqual({r["id"] for r in result["rows"]}, {f"V{i:02}" for i in range(1, 23)})
        self.assertTrue(
            all(r["status"] == r["extension"]["status"] == "unverified" for r in result["rows"])
        )

    def test_missing_or_skipped_mandatory_case_cannot_pass(self):
        for status in ("skip", "unverified", "running"):
            cases = self.passing_cases()
            cases[0]["status"] = status
            self.assertFalse(evaluate(cases)["core_pass"])
        self.assertFalse(evaluate(self.passing_cases()[1:])["core_pass"])

    def test_failed_or_duplicate_evidence_cannot_be_hidden_by_success(self):
        cases = self.passing_cases()
        cases[0]["status"] = "fail"
        self.assertFalse(evaluate(cases)["core_pass"])
        self.assertTrue(any(r["core"]["status"] == "fail" for r in evaluate(cases)["rows"]))
        cases = self.passing_cases()
        self.assertFalse(evaluate(cases + [cases[0]])["core_pass"])

    def test_empty_evidence_cannot_pass(self):
        self.assertFalse(evaluate([])["core_pass"])
        self.assertTrue(all(r["core"]["status"] == "unverified" for r in evaluate([])["rows"]))

    def test_duplicate_python_execution_remains_visible_to_gate(self):
        identity = self.passing_cases()[0]["test"]

        class Repeated(unittest.TestCase):
            def id(self):
                return identity

            def runTest(self):
                pass

        suite = unittest.TestSuite([Repeated(), Repeated()])
        result = unittest.TextTestRunner(stream=io.StringIO(), resultclass=EvidenceResult).run(
            suite
        )
        self.assertEqual(len(result.cases), 2)
        self.assertFalse(evaluate(self.passing_cases()[1:] + result.cases)["core_pass"])

    def test_duplicate_go_execution_remains_visible_to_gate(self):
        identity = next(
            case["test"] for case in self.passing_cases() if case["test"].startswith("go:")
        )
        package, name = identity[3:].split(":", 1)
        line = json.dumps({"Action": "pass", "Package": package, "Test": name})
        cases, package_failed = go_cases(line + "\n" + line)
        self.assertFalse(package_failed)
        self.assertEqual(len(cases), 2)
        self.assertFalse(
            evaluate([case for case in self.passing_cases() if case["test"] != identity] + cases)[
                "core_pass"
            ]
        )
