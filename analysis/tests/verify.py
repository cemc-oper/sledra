"""Run analysis regressions; save evidence only to an explicitly requested new directory."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class EvidenceResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.cases = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.cases.append({"test": test.id(), "status": "pass"})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.cases.append({"test": test.id(), "status": "fail"})

    def addError(self, test, err):
        super().addError(test, err)
        self.cases.append({"test": test.id(), "status": "error"})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report-dir", type=Path, help="new private report directory; never overwrite"
    )
    parser.add_argument(
        "--task",
        choices=("SLD-10", "SLD-11", "SLD-12", "SLD-13", "SLD-14", "SLD-15", "all"),
        default="all",
    )
    args = parser.parse_args()
    if args.report_dir is not None:
        args.report_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
        work = args.report_dir / "work"
        work.mkdir(mode=0o700)
        tempfile.tempdir = str(work.resolve())
    pattern = {
        "SLD-10": "test_source.py",
        "SLD-11": "test_store.py",
        "SLD-12": "test_reader.py",
        "SLD-13": "test_transaction.py",
        "SLD-14": "test_projections.py",
        "SLD-15": "test_metrics.py",
        "all": "test_*.py",
    }[args.task]
    suite = unittest.defaultTestLoader.discover(str(Path(__file__).parent), pattern=pattern)
    result = unittest.TextTestRunner(verbosity=2, resultclass=EvidenceResult).run(suite)
    fixture = ROOT / "tests/m1/results/handoff.json"
    handoff = json.loads(fixture.read_bytes())
    sources = sorted((ROOT / "analysis").rglob("*.py"))
    sources = [path for path in sources if ".venv" not in path.parts and "build" not in path.parts]
    sources.extend(ROOT / "analysis" / name for name in ("Makefile", "pyproject.toml", "uv.lock"))
    report = {
        "task": args.task,
        "matrices": {
            "SLD-10": ["V11"],
            "SLD-11": ["V09"],
            "SLD-12": ["V10", "V16"],
            "SLD-13": ["V12", "V21"],
            "SLD-14": ["V05", "V07", "V08", "V09"],
            "SLD-15": ["V07", "V22"],
            "all": ["V11", "V09", "V10", "V16", "V12", "V21", "V05", "V07", "V08", "V22"],
        }[args.task],
        "analysis_version": "0.1.0.dev0",
        "protocol_version": "1.0.0",
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "platform": platform.platform(),
        "ok": result.wasSuccessful(),
        "tests_run": result.testsRun,
        "cases": result.cases,
        "handoff_sha256": hashlib.sha256(fixture.read_bytes()).hexdigest(),
        "input_files": [
            {"path": item["path"], "sha256": item["sha256"]} for item in handoff["files"]
        ],
        "source_files": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sources
        },
        "memory_observations": getattr(sys.modules.get("test_reader"), "MEMORY_OBSERVATIONS", []),
        "crash_observations": getattr(
            sys.modules.get("test_transaction"), "CRASH_OBSERVATIONS", []
        ),
        "projection_observations": getattr(
            sys.modules.get("test_projections"), "PROPERTY_OBSERVATIONS", []
        ),
        "timing_observations": getattr(
            sys.modules.get("test_metrics"), "PROPERTY_OBSERVATIONS", []
        ),
        "embedded_contracts": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (ROOT / "analysis/src/sledra_analysis/resources/contract").glob("*.json")
        },
        "canonical_fixtures": {
            name: hashlib.sha256((ROOT / "spec/fixtures/1.0.0" / name).read_bytes()).hexdigest()
            for name in (
                "valid-bundle.json",
                "golden-vectors.json",
                "invalid-conflicting-event-id.json",
            )
        },
        "unverified": [
            "real shared/HPC filesystem",
            "real inode allocator reuse (hint simulated)",
            "hardware power loss and target shared-filesystem durability",
            "production retention policy, deployment and cross-node cleanup",
            "A6/A7 qualification and full M2 closure",
            "legacy arbitrary equal-length rewrite detection (unprovable)",
        ],
    }
    if args.report_dir is not None:
        (args.report_dir / "validation.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps({"ok": report["ok"], "tests_run": result.testsRun}))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
