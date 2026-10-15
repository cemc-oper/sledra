"""Collect every Python and Go test outcome for the core gate."""

from __future__ import annotations

import json
import unittest


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []

    def startTest(self, test):
        super().startTest(test)
        self.case = {"test": test.id(), "status": "running"}
        self.cases.append(self.case)
        root = getattr(test, "root", None)
        if root:
            self.case["work"] = str(root)

    def stopTest(self, test):
        root = getattr(test, "root", None)
        if root:
            self.case["work"] = str(root)
        super().stopTest(test)

    def addSuccess(self, test):
        super().addSuccess(test)
        self.case["status"] = "pass"

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.case.update(status="fail", detail=self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self.case.update(status="error", detail=self._exc_info_to_string(err, test))

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.case.update(status="skip", detail=reason)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err:
            self.case.update(
                status="fail", detail=self._exc_info_to_string(err, test), subtest=str(subtest)
            )


def go_cases(raw):
    """Top-level and subtest identities remain separate; package failures also gate."""
    cases = []
    package_failed = False
    for line in raw.splitlines():
        value = json.loads(line)
        if value.get("Action") not in ("pass", "fail", "skip"):
            continue
        if "Test" in value:
            identity = "go:" + value["Package"] + ":" + value["Test"]
            cases.append({"test": identity, "status": value["Action"]})
        elif value["Action"] == "fail":
            package_failed = True
    return cases, package_failed
