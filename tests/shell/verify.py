"""SLD-08 local Bash baseline comparison; no scheduler or analyzer is used."""

from __future__ import annotations

import argparse
import json
import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT))
from scripts.test_session import TestSession, add_arguments  # noqa: E402

EXAMPLE = ROOT / "examples/standalone/run.sh"
LIBRARY = ROOT / "integrations/bash/sledra.sh"


class ShellIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.binary = cls.session.binary

    def setup_case(
        self, *, enabled: bool, mode: str = "success", fault: str = ""
    ) -> tuple[Path, dict[str, str]]:
        base = Path(tempfile.mkdtemp(prefix="case-", dir=self.session.work))
        root = base / "observability"
        root.mkdir()
        env = dict(
            self.session.env,
            STANDALONE_WORK_DIR=str(base),
            SLEDRA_ROOT=str(root),
            SLEDRA_BIN=str(self.binary),
            SLEDRA_ENABLED="1" if enabled else "0",
            STANDALONE_PAYLOAD_MODE=mode,
        )
        if fault == "missing_tool":
            env["SLEDRA_BIN"] = str(base / "missing-sledra")
        elif fault == "missing_context":
            env["STANDALONE_CONTEXT_OVERRIDE"] = str(root / "contexts/missing.json")
        elif fault == "spool_permission":
            spool = root / "spool"
            spool.mkdir()
            spool.chmod(0o500)
        elif fault == "write_failure":
            fake = base / "fault-sledra"
            fake.write_text(
                "#!/usr/bin/env bash\n"
                "if [[ ${1:-} == event ]]; then exit 73; fi\n"
                'exec "$REAL_SLEDRA" "$@"\n'
            )
            fake.chmod(0o700)
            env["SLEDRA_BIN"] = str(fake)
            env["REAL_SLEDRA"] = str(self.binary)
        return base, env

    def run_case(self, *, enabled: bool, mode: str = "success", fault: str = "") -> dict:
        base, env = self.setup_case(enabled=enabled, mode=mode, fault=fault)
        result = subprocess.run([str(EXAMPLE)], env=env, capture_output=True, timeout=20)
        return self.result(base, result.returncode, result.stdout, result.stderr)

    @staticmethod
    def result(base: Path, code: int, stdout: bytes, stderr: bytes) -> dict:
        execution = base / "executions.log"
        callback = base / "callbacks.log"
        events = []
        for path in (base / "observability").rglob("*.jsonl"):
            for line in path.read_bytes().splitlines()[1:]:
                events.append(json.loads(line))
        return {
            "code": code,
            "stdout": stdout,
            "stderr": stderr,
            "executions": execution.read_text().splitlines() if execution.exists() else [],
            "callbacks": callback.read_text().splitlines() if callback.exists() else [],
            "events": events,
            "product": (base / "product.txt").exists(),
            "contexts": sorted(p.name for p in (base / "observability/contexts").glob("*.json")),
            "finish_count": len(list((base / "observability/finish").rglob("*.json"))),
        }

    def assert_business_equal(self, before: dict, after: dict, *, diagnostic: bool = False) -> None:
        for key in ("code", "stdout", "executions", "callbacks", "product"):
            self.assertEqual(before[key], after[key], key)
        if diagnostic:
            marker = b'{"ok":false,"code":"shell_observation_unavailable"}\n'
            self.assertEqual(after["stderr"].count(marker), 1)
            self.assertEqual(after["stderr"].replace(marker, b"", 1), before["stderr"])
        else:
            self.assertEqual(before["stderr"], after["stderr"])
        self.assertEqual(after["executions"], ["run"])

    def test_success_and_failure(self) -> None:
        for mode, code, types in (
            ("success", 0, ["attempt.exited", "attempt.started", "stage.ended", "stage.started"]),
            ("fail", 7, ["attempt.exited", "attempt.started", "stage.started"]),
        ):
            with self.subTest(mode=mode):
                before = self.run_case(enabled=False, mode=mode)
                after = self.run_case(enabled=True, mode=mode)
                self.assert_business_equal(before, after)
                self.assertEqual(after["code"], code)
                self.assertEqual(sorted(e["event_type"] for e in after["events"]), types)
                self.assertEqual(after["contexts"], ["attempt.json", "run.json"])
                self.assertEqual(after["finish_count"], 1 if mode == "success" else 0)
                exit_event = next(e for e in after["events"] if e["event_type"] == "attempt.exited")
                self.assertEqual(exit_event["payload"]["exit_code"], code)
                self.assertEqual(exit_event["payload"]["termination"], "exited")

    def test_observation_faults_do_not_repeat_payload(self) -> None:
        for fault in ("missing_tool", "missing_context", "spool_permission", "write_failure"):
            for mode in ("success", "fail"):
                with self.subTest(fault=fault, mode=mode):
                    before = self.run_case(enabled=False, mode=mode)
                    after = self.run_case(enabled=True, mode=mode, fault=fault)
                    self.assert_business_equal(before, after, diagnostic=True)
                    self.assertNotIn(b"synthetic product", after["stderr"])

    def signal_case(self, *, enabled: bool, number: int, probe_int: bool = False) -> dict:
        base, env = self.setup_case(enabled=enabled, mode="hang")
        process = subprocess.Popen(
            [str(EXAMPLE)],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            bufsize=0,
        )
        prefix = bytearray()
        deadline = time.monotonic() + 12
        try:
            while b"payload-ready\n" not in prefix:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, "payload did not become ready")
                readable, _, _ = select.select([process.stdout], [], [], remaining)
                self.assertTrue(readable, "payload stdout timeout")
                chunk = os.read(process.stdout.fileno(), 4096)
                self.assertTrue(chunk, "payload exited before signal")
                prefix.extend(chunk)
            if number == signal.SIGKILL:
                os.killpg(process.pid, number)
            else:
                os.kill(process.pid, number)
            if probe_int:
                time.sleep(0.2)
                self.assertIsNone(process.poll(), "INT behavior changed on this Bash profile")
                os.killpg(process.pid, signal.SIGKILL)
            rest, stderr = process.communicate(timeout=8)
            return self.result(base, process.returncode, bytes(prefix) + rest, stderr)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()

    def test_term_hup_and_kill(self) -> None:
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGKILL):
            with self.subTest(signal=number):
                before = self.signal_case(enabled=False, number=number)
                after = self.signal_case(enabled=True, number=number)
                self.assert_business_equal(before, after)
                self.assertEqual(after["code"], -number)
                exits = [e for e in after["events"] if e["event_type"] == "attempt.exited"]
                if number == signal.SIGKILL:
                    self.assertFalse(exits)
                else:
                    self.assertEqual(len(exits), 1)
                    self.assertEqual(exits[0]["payload"]["signal"], number)
                    self.assertEqual(exits[0]["payload"]["termination"], "signaled")

    def test_int_profile_is_not_qualified(self) -> None:
        before = self.signal_case(enabled=False, number=signal.SIGINT, probe_int=True)
        after = self.signal_case(enabled=True, number=signal.SIGINT, probe_int=True)
        self.assert_business_equal(before, after)
        self.assertEqual(after["code"], -signal.SIGKILL)

    def test_source_preserves_options_traps_and_pipeline(self) -> None:
        for mask in range(8):
            settings = [
                option
                for bit, option in enumerate(("-e", "-u", "-o pipefail"))
                if mask & (1 << bit)
            ]
            setup = "\n".join("set " + option for option in settings)
            script = f"""
                {setup}
                trap 'printf callback >&2' EXIT
                before_flags=$-
                before_pipe=$(set -o | grep '^pipefail')
                before_trap=$(trap -p EXIT)
                source {shlex.quote(str(LIBRARY))}
                sledra_attempt_exited 7 forecast_program
                [[ $- == "$before_flags" ]]
                [[ $(set -o | grep '^pipefail') == "$before_pipe" ]]
                [[ $(trap -p EXIT) == "$before_trap" ]]
                printf 'pipeline\\n' | cat
            """
            with self.subTest(mask=mask):
                result = subprocess.run(
                    ["bash", "-c", script],
                    env=dict(os.environ, SLEDRA_ENABLED="0"),
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout, b"pipeline\n")
                self.assertEqual(result.stderr, b"callback")

        pipeline = 'set +e; set -o pipefail; false | true; status=("${PIPESTATUS[@]}"); printf \'%s\\n\' "${status[*]}"'
        baseline = subprocess.run(["bash", "-c", pipeline], capture_output=True, check=True)
        observed = subprocess.run(
            ["bash", "-c", f"source {shlex.quote(str(LIBRARY))}; {pipeline}"],
            capture_output=True,
            check=True,
        )
        self.assertEqual(baseline.stdout, b"1 0\n")
        self.assertEqual(observed.stdout, baseline.stdout)

    def test_unqualified_supervisor_is_unavailable(self) -> None:
        base, env = self.setup_case(enabled=True)
        env["SLEDRA_SPEC_DIR"] = str(self.session.spec)
        result = subprocess.run(
            [str(self.binary), "exec", "--", "/bin/true"], capture_output=True, env=env
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(json.loads(result.stderr), {"ok": False, "code": "usage"})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    add_arguments(parser)
    args = parser.parse_args()
    with TestSession(args, "shell") as session:
        if shutil.which("grep") is None:
            raise ValueError("missing Shell test dependency: grep")
        ShellIntegration.session = session
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(ShellIntegration)
        if session.report is not None:
            with (session.report / "assertions.log").open("w") as log:
                result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        else:
            result = unittest.TextTestRunner(stream=sys.stderr, verbosity=2).run(suite)
        session.data.update(
            sample_count=result.testsRun,
            failures=len(result.failures),
            errors=len(result.errors),
            cases=[str(p) for p in session.work.glob("case-*")],
        )
        if not result.wasSuccessful():
            raise AssertionError("Shell assertions failed")
