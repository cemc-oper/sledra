"""Exercise the recorder without source resources from a read-only installation."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts.test_tools import binary_for  # noqa: E402


def inventory(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


class PortableCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory(prefix="sledra cli installation ")
        cls.base = Path(cls.temp.name)
        cls.install = cls.base / "read only installation"
        cls.install.mkdir()
        cls.binary = cls.install / "bin/sledra"
        cls.binary.parent.mkdir()
        shutil.copyfile(binary_for("sledra", cls.base), cls.binary)
        cls.binary.chmod(0o555)
        cls.share = cls.install / "share/sledra"
        for directory in ("integrations/bash", "examples/standalone"):
            shutil.copytree(ROOT / directory, cls.share / directory)
        for path in sorted(cls.install.rglob("*"), reverse=True):
            path.chmod(
                0o555 if path.is_dir() or path.suffix == ".sh" or path == cls.binary else 0o444
            )
        cls.install.chmod(0o555)
        cls.initial = inventory(cls.install)
        cls.caller = cls.base / "unrelated cwd"
        cls.caller.mkdir()
        cls.env = {
            k: v for k, v in os.environ.items() if not k.startswith(("SLEDRA_", "STANDALONE_"))
        }

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            assert inventory(cls.install) == cls.initial, "read-only installation bytes changed"
            assert not (cls.share / "spec").exists()
        finally:
            for path in cls.install.rglob("*"):
                if path.is_dir():
                    path.chmod(0o755)
            cls.install.chmod(0o755)
            cls.temp.cleanup()

    def call(
        self, args: list[str], env: dict[str, str] | None = None, cwd: Path | None = None
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [str(self.binary), *args],
            cwd=cwd or self.caller,
            env=env or self.env,
            capture_output=True,
            timeout=20,
        )

    def test_help_and_version_without_environment(self) -> None:
        for topic in (
            [],
            ["registry"],
            ["registry", "run-create"],
            ["registry", "attempt-create"],
            ["context"],
            ["context", "validate"],
            ["run"],
            ["run", "finish"],
            ["event"],
        ):
            for option in ("--help", "-h"):
                result = self.call([*topic, option])
                self.assertEqual((result.returncode, result.stderr), (0, b""))
                self.assertIn(b"Usage:", result.stdout)
        first = self.call(["--version"])
        second = self.call(["--version"], cwd=self.base)
        self.assertEqual((first.returncode, first.stderr, second.stdout), (0, b"", first.stdout))
        value = json.loads(first.stdout)
        self.assertEqual(value["contract_version"], "1.0.0")
        for key in (
            "software_version",
            "commit",
            "build_time",
            "source_sha256",
            "go_version",
            "goos",
            "goarch",
        ):
            self.assertTrue(value[key], key)
        self.assertEqual(value["goos"], "linux")

    def test_explicit_bad_override_does_not_affect_information(self) -> None:
        env = dict(self.env, SLEDRA_ROOT="/missing", SLEDRA_SPEC_DIR="relative/bad")
        for args in (["--version"], ["event", "--help"], ["help", "registry", "run-create"]):
            result = self.call(args, env)
            self.assertEqual((result.returncode, result.stderr), (0, b""))

    def test_original_usage_and_missing_root_protocol(self) -> None:
        for args, code in (([], "usage"), (["registry", "run-create"], "root_missing")):
            result = self.call(args)
            self.assertEqual((result.returncode, result.stdout), (1, b""))
            self.assertEqual(json.loads(result.stderr), {"ok": False, "code": code})

    def test_embedded_lifecycle_from_multiple_directories(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.base, prefix="data ") as tmp:
            work = Path(tmp)
            root = work / "observability"
            root.mkdir()
            env = dict(self.env, SLEDRA_ROOT=str(root))
            request = json.loads(
                (self.share / "examples/standalone/fixtures/run-request.json").read_text()
            )
            for key in ("definition_file", "expectation_file"):
                if key in request:
                    request[key] = str(self.share / "examples/standalone" / request[key])
            req = work / "run request.json"
            req.write_text(json.dumps(request))
            context = root / "contexts/run.json"
            for cwd in (self.caller, self.base):
                for args in (
                    [
                        "registry",
                        "run-create",
                        "--request-file",
                        str(req),
                        "--context-out",
                        str(context),
                    ],
                    ["context", "validate", "--file", str(context)],
                ):
                    result = self.call(args, env, cwd)
                    self.assertEqual(
                        (result.returncode, result.stdout, result.stderr), (0, b"", b"")
                    )
            self.assertFalse(
                os.access(self.install, os.W_OK), "installation must actually be read-only"
            )

    def test_override_failures_keep_event_business_and_strict_policy(self) -> None:
        with tempfile.TemporaryDirectory(dir=self.base) as tmp:
            root = Path(tmp)
            spec = root / "override spec"
            shutil.copytree(ROOT / "spec/1.0.0", spec)
            # Same JSON with different formatting remains compatible.
            schema = spec / "run_context.schema.json"
            schema.write_text(schema.read_text() + " \n")
            env = dict(
                self.env,
                SLEDRA_ROOT=str(root),
                SLEDRA_SPEC_DIR=str(spec),
                SLEDRA_CONFIG=str(self.share / "examples/standalone/fixtures/process-config.json"),
            )
            args = ["context", "validate", "--file", str(root / "missing.json")]
            self.assertEqual(json.loads(self.call(args, env).stderr)["code"], "context_missing")
            for override, code in (
                (str(root / "missing spec"), "contract_unavailable"),
                ("relative/spec", "contract_path_invalid"),
                (str(spec), "contract_incompatible"),
            ):
                schema.write_text('{"secret":"must-not-appear"}')
                env["SLEDRA_SPEC_DIR"] = override
                lifecycle = self.call(args, env)
                self.assertEqual((lifecycle.returncode, lifecycle.stdout), (1, b""))
                self.assertEqual(json.loads(lifecycle.stderr), {"ok": False, "code": code})
                event = [
                    "event",
                    "--context",
                    str(root / "missing.json"),
                    "--type",
                    "attempt.started",
                    "--invocation-id",
                    "cli-test",
                    "--process-role",
                    "forecast_program",
                ]
                business = self.call(event, env)
                self.assertEqual((business.returncode, business.stdout), (0, b""))
                self.assertEqual(
                    json.loads(business.stderr), {"ok": False, "code": code, "dropped": 1}
                )
                strict = self.call([*event, "--strict"], env)
                self.assertEqual((strict.returncode, strict.stdout), (1, b""))
                self.assertEqual(json.loads(strict.stderr), {"ok": False, "code": code})

    def test_detached_shell_example_preserves_payload_behavior(self) -> None:
        for mode in ("success", "fail"):
            outcomes = []
            for enabled in ("0", "1"):
                work = self.base / f"owner-{mode}-{enabled}"
                (work / "observability").mkdir(parents=True)
                env = dict(
                    self.env,
                    STANDALONE_WORK_DIR=str(work),
                    SLEDRA_BIN=str(self.binary),
                    SLEDRA_ENABLED=enabled,
                    STANDALONE_PAYLOAD_MODE=mode,
                )
                result = subprocess.run(
                    [str(self.share / "examples/standalone/run.sh")],
                    env=env,
                    cwd=self.caller,
                    capture_output=True,
                    timeout=20,
                )
                outcomes.append(
                    (
                        result.returncode,
                        result.stdout,
                        result.stderr,
                        (work / "executions.log").read_bytes(),
                        (work / "callbacks.log").read_bytes(),
                        (
                            (work / "product.txt").read_bytes()
                            if (work / "product.txt").exists()
                            else None
                        ),
                    )
                )
                if enabled == "1":
                    root = work / "observability"
                    self.assertTrue((root / "contexts/run.json").is_file())
                    self.assertTrue((root / "contexts/attempt.json").is_file())
                    events = [
                        json.loads(line)
                        for p in root.glob("spool/**/*.jsonl")
                        for line in p.read_text().splitlines()
                    ]
                    self.assertIn("attempt.started", {e.get("event_type") for e in events})
                    self.assertIn("attempt.exited", {e.get("event_type") for e in events})
            self.assertEqual(outcomes[0], outcomes[1])
            self.assertEqual(outcomes[1][0], 0 if mode == "success" else 7)


if __name__ == "__main__":
    unittest.main(verbosity=2)
