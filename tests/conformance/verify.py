"""Run fresh M2 core evidence and gate all V01--V22 scopes, without production inputs."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "analysis/tests"))

from flow import HANDOFF_HASH, save  # noqa: E402
from matrix import evaluate  # noqa: E402
from scripts.test_session import TestSession, add_arguments, source_identity  # noqa: E402
import test_core  # noqa: E402


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = {}

    def startTest(self, test):
        super().startTest(test)
        self.cases[test.id()] = {"test": test.id(), "status": "running"}
        root = getattr(test, "root", None)
        if root:
            self.cases[test.id()]["work"] = str(root)

    def stopTest(self, test):
        root = getattr(test, "root", None)
        if root:
            self.cases[test.id()]["work"] = str(root)
        super().stopTest(test)

    def addSuccess(self, test):
        super().addSuccess(test)
        self.cases[test.id()]["status"] = "pass"

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.cases[test.id()].update(status="fail", detail=self._exc_info_to_string(err, test))

    def addError(self, test, err):
        super().addError(test, err)
        self.cases.setdefault(test.id(), {"test": test.id()}).update(
            status="error", detail=self._exc_info_to_string(err, test)
        )

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self.cases[test.id()].update(status="skip", detail=reason)

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err:
            self.cases[test.id()].update(
                status="fail", detail=self._exc_info_to_string(err, test), subtest=str(subtest)
            )


def go_cases(raw):
    """Top-level and subtest identities remain separate; package failures also gate."""
    cases = {}
    package_failed = False
    for line in raw.splitlines():
        value = json.loads(line)
        if value.get("Action") not in ("pass", "fail", "skip"):
            continue
        if "Test" in value:
            identity = "go:" + value["Package"] + ":" + value["Test"]
            cases[identity] = {"test": identity, "status": value["Action"]}
        elif value["Action"] == "fail":
            package_failed = True
    return list(cases.values()), package_failed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    args = parser.parse_args()
    if args.mode != "local":
        parser.error("SLD-23 core is synthetic/local only; HPC qualification is a separate task")
    with TestSession(args, "conformance") as session:
        head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        # TestSession owns default cleanup and retains a failed run. Explicit
        # evidence is kept in the requested new report tree, including databases.
        work = (session.report / "work") if session.report else session.work / "evidence"
        work.mkdir(mode=0o700)
        tempfile.tempdir = str(work)
        test_core.ARTIFACT_ROOT = work / "core"
        test_core.BINARY, test_core.SPEC = session.binary, session.spec
        module_spec = importlib.util.spec_from_file_location(
            "sledra_shell", ROOT / "tests/shell/verify.py"
        )
        shell = importlib.util.module_from_spec(module_spec)
        sys.modules["sledra_shell"] = shell
        module_spec.loader.exec_module(shell)
        shell.ShellIntegration.session = session
        comparisons = []
        original_compare = shell.ShellIntegration.assert_business_equal

        def compare(owner, before, after, *, diagnostic=False):
            original_compare(owner, before, after, diagnostic=diagnostic)
            comparisons.append(
                {
                    "test": owner.id(),
                    "rc_before": before["code"],
                    "rc_after": after["code"],
                    "stdout_sha256": hashlib.sha256(after["stdout"]).hexdigest(),
                    "stderr_before_sha256": hashlib.sha256(before["stderr"]).hexdigest(),
                    "stderr_after_sha256": hashlib.sha256(after["stderr"]).hexdigest(),
                    "bounded_observation_diagnostic": diagnostic,
                    "callbacks": after["callbacks"],
                    "executions": after["executions"],
                    "product": after["product"],
                    "event_types": sorted(e["event_type"] for e in after["events"]),
                }
            )

        shell.ShellIntegration.assert_business_equal = compare
        suite = unittest.TestSuite(
            [
                unittest.TestLoader().discover(str(ROOT / "analysis/tests")),
                unittest.TestLoader().discover(
                    str(ROOT / "tests/conformance"), pattern="test_*.py"
                ),
                unittest.defaultTestLoader.loadTestsFromTestCase(shell.ShellIntegration),
            ]
        )
        result = unittest.TextTestRunner(verbosity=2, resultclass=EvidenceResult).run(suite)
        cases = list(result.cases.values())
        go = session.run(
            ["go", "test", "-count=1", "-json", "./..."],
            cwd=ROOT / "recorder",
            env=session.env
            | {
                "GOPROXY": "off",
                "GOSUMDB": "off",
                "GONOPROXY": "none",
                "GOTOOLCHAIN": "local",
                "CGO_ENABLED": "0",
            },
            timeout=180,
        )
        native_cases, package_failed = go_cases(go.stdout)
        cases.extend(native_cases)
        matrix = evaluate(cases)
        unchanged = source_identity() == session.data["input"]
        fixture_hash = hashlib.sha256(
            (ROOT / "tests/m1/results/handoff.json").read_bytes()
        ).hexdigest()
        ok = (
            result.wasSuccessful()
            and go.returncode == 0
            and not package_failed
            and matrix["core_pass"]
            and unchanged
            and fixture_hash == HANDOFF_HASH
        )
        data = {
            "task": "SLD-23",
            "scope": "M2 synthetic local core",
            "status": "pass" if ok else "fail",
            "source": session.data["input"],
            "head": head,
            "versions": {
                "analysis": "0.1.0.dev0",
                "protocol": "1.0.0",
                "raw_schema": 3,
                "projection_schema": 1,
                "timing_schema": 1,
                "state_reducer": "sledra-state-reducer-1.0.0",
                "state_rules": "sledra-state-rules-1.0.0",
                "timing_rules": "sledra-timing-1.0.0",
            },
            "tools": {
                "python": platform.python_version(),
                "sqlite": sqlite3.sqlite_version,
                "go": session.run(["go", "version"]).stdout.strip(),
                "bash": session.data["bash"],
            },
            "binary": session.data["binary"],
            "spec": session.data["spec"],
            "platform": session.data["platform"],
            "mount": session.data["mount"],
            "handoff_sha256": fixture_hash,
            "source_unchanged_during_run": unchanged,
            "python_tests": result.testsRun,
            "go_cases": len(native_cases),
            "cases": cases,
            "matrix": matrix,
            "core_observations": test_core.OBSERVATIONS,
            "business_comparisons": comparisons,
            "synthetic_live_payload_executions": 4,
            "retained_artifacts": {
                str(path.relative_to(work)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in sorted(work.rglob("*"))
                if path.is_file()
                and path.name
                in {
                    "inputs.json",
                    "event-diff.json",
                    "killed-at.json",
                    "result.json",
                    "facts.sqlite",
                    "restored.sqlite",
                    "fault-locations.json",
                    "business-diff.json",
                    "mock-submit-result.json",
                    "offline-result.json",
                }
            },
            "memory_observations": getattr(
                sys.modules.get("test_reader"), "MEMORY_OBSERVATIONS", []
            ),
            "prior_crash_observations": {
                module: getattr(sys.modules.get(module), name, [])
                for module, name in (
                    ("test_transaction", "CRASH_OBSERVATIONS"),
                    ("test_projections", "PROPERTY_OBSERVATIONS"),
                    ("test_metrics", "PROPERTY_OBSERVATIONS"),
                )
            },
            "full_matrix_pass": False,
            "production_jobs_executed": 0,
            "unverified": [
                "M3/M4 integration and product rules",
                "real shared filesystem, node loss and HPC",
                "production retention cleanup/distribution",
                "hardware power loss",
                "immediate INT qualification and generic supervisor",
                "A6/A7 and remote CI",
            ],
        }
        session.data.update(
            sample_count=result.testsRun + len(native_cases),
            core_matrix=matrix["core_pass"],
            full_matrix=False,
        )
        if session.report:
            save(session.report / "validation.json", data)
            save(session.report / "matrix.json", matrix)
            (session.report / "go-test.jsonl").write_text(go.stdout)
        print(
            json.dumps(
                {
                    "task": "SLD-23",
                    "ok": ok,
                    "python_tests": result.testsRun,
                    "core_matrix_pass": matrix["core_pass"],
                    "full_matrix_pass": False,
                }
            )
        )
        if not ok:
            raise AssertionError(
                "SLD-23 core gate failed; inspect cases, matrix and retained fixture locations"
            )


if __name__ == "__main__":
    main()
