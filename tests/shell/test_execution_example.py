"""P1 acceptance: actual standalone business behavior with real Go/Bash hooks."""

from __future__ import annotations

import hashlib
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples/execution/standalone.sh"
LIBRARY = ROOT / "integrations/bash/sledra-execution.sh"
DIAG = b'{"ok":false,"code":"execution_observation_unavailable"}\n'
sys.path.insert(0, str(ROOT))
from scripts.test_tools import binary_for  # noqa: E402

sys.path.insert(0, str(ROOT / "spec/execution-v2"))
from check import execution_path, stream_conflicts, validate  # noqa: E402


class ExecutionExample(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build = tempfile.TemporaryDirectory(prefix="sledra example build ")
        cls.addClassCleanup(cls.build.cleanup)
        cls.binary = binary_for("sledra", Path(cls.build.name))

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sledra example acceptance ")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("SLEDRA_", "EXAMPLE_", "BUSINESS_", "BASH_FUNC_"))
            and k not in {"BASH_ENV", "ENV"}
        }
        self.configure = self.work / "configure.sh"
        self.configure.write_text(r"""#!/bin/bash
printf 'configure\n' >> "$CONFIGURE_LOG"
printf 'configure\n' >> "$TIMELINE"
exit "${CONFIGURE_RC:-0}"
""")
        self.configure.chmod(0o700)
        self.payload = self.work / "payload.sh"
        self.payload.write_text(r"""#!/bin/bash
set -euo pipefail
trap 'exit 143' TERM
printf 'run\n' >> "$BUSINESS_LOG"
printf '%s\0' "$@" >> "$BUSINESS_ARGV"
printf 'business\n' >> "$TIMELINE"
printf 'business stdout\n'
printf 'business stderr\n' >&2
if [[ ${BUSINESS_WAIT:-0} == 1 ]]; then
    printf 'ready\n' >&"$BUSINESS_READY_FD"
    IFS= read -r _ <&"$BUSINESS_WAIT_FD"
fi
if [[ ${BUSINESS_RC:-0} != 0 ]]; then exit "$BUSINESS_RC"; fi
printf 'product bytes\n' > "$PRODUCT"
""")
        self.payload.chmod(0o700)

    def invoke(
        self,
        mode: str = "execution-v2",
        *,
        fault: str = "",
        settings=None,
        signum: int | None = None,
        shared_root: Path | None = None,
        no_command: bool = False,
        installation: Path | None = None,
        filename_only: bool = False,
    ) -> dict:
        work = Path(tempfile.mkdtemp(prefix="case-", dir=self.work))
        root = shared_root or work / "records"
        binary = installation / "bin/sledra" if installation else self.binary
        example = installation / "examples/execution/standalone.sh" if installation else EXAMPLE
        subprocess.run(
            [
                str(binary),
                "execution",
                "init-root",
                "--root",
                str(root),
                "--system",
                "synthetic",
                "--deployment",
                "local",
                "--partition-timezone=+08:00",
            ],
            env=self.env,
            cwd=work,
            capture_output=True,
            check=True,
            timeout=15,
        )
        config = validate("root", (root / "sledra.json").read_bytes())
        env = (
            self.env
            | {
                "SLEDRA_MODE": mode,
                "SLEDRA_ROOT": str(root),
                "SLEDRA_CONFIG": "/must-not-load-producer",
                "SLEDRA_SPEC_DIR": "bad-relative-legacy-spec",
                "SLEDRA_RUN_ID": "stale-inherited-run",
                "ECF_NAME": "/must/not/infer",
                "EXAMPLE_CONFIGURE": str(self.configure),
                "EXAMPLE_CALLBACK_LOG": str(work / "callbacks"),
                "CONFIGURE_LOG": str(work / "configure"),
                "BUSINESS_LOG": str(work / "business"),
                "BUSINESS_ARGV": str(work / "argv"),
                "TIMELINE": str(work / "timeline"),
                "PRODUCT": str(work / "product"),
                "CLI_LOG": str(work / "cli"),
                "REAL_SLEDRA": str(binary),
                "FAULT": fault,
            }
            | (settings or {})
        )
        wrapper = work / "recorder"
        wrapper.write_text(r"""#!/bin/bash
printf '%s %s %s\n' "$1" "${2:-}" "${3:-}" >> "$CLI_LOG"
printf 'recorder:%s:%s\n' "${2:-}" "${3:-}" >> "$TIMELINE"
if [[ $2 == prepare && ${3:-} == event && $FAULT == malformed ]]; then
    printf 'invalid receipt\n'
    exit 0
fi
if [[ $2 == append ]]; then
    IFS= read -r line || exit 3
    if [[ $line == *'"name":"example.progress"'* ]]; then
        files=("$SLEDRA_ROOT"/executions/*/*/*.jsonl)
        case $FAULT in
            short) printf '%s' "${line:0:7}" >> "${files[0]}"; exit 3 ;;
            complete_error|close_error)
                "$REAL_SLEDRA" "$@" <<< "$line"
                printf '{"ok":false,"code":"indeterminate"}\n' >&2
                exit 3 ;;
            tail) printf partial >> "${files[0]}" ;;
            tool_signal) kill -KILL "$BASHPID" ;;
        esac
    fi
    exec "$REAL_SLEDRA" "$@" <<< "$line"
fi
exec "$REAL_SLEDRA" "$@"
""")
        wrapper.chmod(0o700)
        env["SLEDRA_BIN"] = str(wrapper)
        if fault == "missing_tool":
            env["SLEDRA_BIN"] = str(work / "not-installed")
        elif fault == "permission":
            (root / "executions").mkdir()
            (root / "executions").chmod(0o500)
        elif fault == "root_incompatible":
            (root / "sledra.json").write_text('{"format_version":"9.9.9"}')
        argv = [
            "space value",
            "",
            "中文",
            "$(touch must-not-run); `touch must-not-run`",
            "line\nbreak",
        ]
        command = [] if no_command else [str(self.payload), *argv]
        if env.get("BUSINESS_FUNCTION"):
            # A real function in the owner tests errexit/pipefail sensitivity to
            # accidentally wrapping business execution in if/|| contexts.
            env["BASH_FUNC_owned_payload%%"] = r"""() {
    printf 'run\n' >> "$BUSINESS_LOG"
    printf '%s\0' "$@" >> "$BUSINESS_ARGV"
    printf 'business\n' >> "$TIMELINE"
    if [[ $BUSINESS_FUNCTION == pipeline ]]; then false | true; else false; fi
    printf 'must-not-reach\n' > "$PRODUCT"
}"""
            command = ["owned_payload", *argv]
        if signum is None:
            result = subprocess.run(
                ["/bin/bash", example.name if filename_only else str(example), *command],
                env=env,
                cwd=example.parent if filename_only else work,
                capture_output=True,
                timeout=20,
            )
        else:
            ready_r, ready_w = os.pipe()
            wait_r, wait_w = os.pipe()
            env |= {
                "BUSINESS_WAIT": "1",
                "BUSINESS_READY_FD": str(ready_w),
                "BUSINESS_WAIT_FD": str(wait_r),
            }
            child = subprocess.Popen(
                ["/bin/bash", str(example), *command],
                env=env,
                cwd=work,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(ready_w, wait_r),
                start_new_session=True,
            )
            os.close(ready_w)
            os.close(wait_r)
            try:
                if (
                    not select.select([ready_r], [], [], 15)[0]
                    or os.read(ready_r, 64) != b"ready\n"
                ):
                    self.fail("business did not reach the signal barrier")
                os.killpg(child.pid, signum)
                stdout, stderr = child.communicate(timeout=15)
                result = subprocess.CompletedProcess(child.args, child.returncode, stdout, stderr)
            finally:
                if child.poll() is None:
                    os.killpg(child.pid, signal.SIGKILL)
                    child.communicate(timeout=10)
                os.close(ready_r)
                os.close(wait_w)
        if fault == "permission":
            (root / "executions").chmod(0o700)

        def data(name: str) -> bytes:
            return (work / name).read_bytes() if (work / name).exists() else b""

        return {
            "root": root,
            "config": config,
            "work": work,
            "rc": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "business": data("business"),
            "argv": data("argv"),
            "callbacks": data("callbacks"),
            "configure": data("configure"),
            "product": data("product"),
            "cli": data("cli").decode().splitlines(),
            "timeline": data("timeline").decode().splitlines(),
            "expected_argv": b"\0".join(a.encode() for a in argv) + b"\0",
        }

    def assert_equivalent(self, baseline: dict, observed: dict, *, fault: bool = False) -> None:
        for field in ["rc", "stdout", "business", "argv", "callbacks", "configure", "product"]:
            self.assertEqual(observed[field], baseline[field], field)
        self.assertEqual(observed["stderr"].count(DIAG), int(fault))
        self.assertEqual(observed["stderr"].replace(DIAG, b""), baseline["stderr"])
        self.assertEqual(
            [x for x in observed["timeline"] if not x.startswith("recorder:")], baseline["timeline"]
        )
        self.assertFalse((observed["work"] / "must-not-run").exists())

    def histories(self, result: dict, *, partial: bool = False) -> list[list[dict]]:
        paths = sorted(result["root"].glob("executions/*/*/*.jsonl"))
        self.assertEqual(
            {p for p in result["root"].rglob("*") if p.is_file()},
            set(paths) | {result["root"] / "sledra.json"},
        )
        histories = []
        for path in paths:
            raw = path.read_bytes()
            if partial:
                self.assertFalse(raw.endswith(b"\n"))
                raw = raw[: raw.rfind(b"\n") + 1]
            self.assertEqual(stream_conflicts(raw), [])
            events = [validate("event", line) for line in raw.splitlines()]
            self.assertEqual(
                str(path.relative_to(result["root"])),
                execution_path(
                    events[0]["execution_id"], events[0]["occurred_at"], result["config"]
                ),
            )
            self.assertTrue(all(e["execution_id"] == events[0]["execution_id"] for e in events))
            histories.append(events)
        return histories

    def test_normal_failure_and_owner_functions_preserve_business_baseline(self) -> None:
        for settings in [
            {},
            {"BUSINESS_RC": "7"},
            {"BUSINESS_FUNCTION": "errexit"},
            {"BUSINESS_FUNCTION": "pipeline"},
        ]:
            with self.subTest(settings=settings):
                baseline = self.invoke("off", settings=settings)
                observed = self.invoke(settings=settings)
                self.assert_equivalent(baseline, observed)
                self.assertEqual(baseline["cli"], [])
                self.assertEqual(self.histories(baseline), [])
                self.assertEqual(observed["business"], b"run\n")
                self.assertEqual(observed["argv"], observed["expected_argv"])
                events = self.histories(observed)[0]
                self.assertEqual(
                    [e["name"] for e in events],
                    [
                        "execution.started",
                        "execution.context",
                        "stage.started",
                        "example.progress",
                        "stage.ended",
                        "execution.ended",
                    ],
                )
                code = (
                    1 if "BUSINESS_FUNCTION" in settings else int(settings.get("BUSINESS_RC", "0"))
                )
                self.assertEqual(observed["rc"], code)
                self.assertEqual(events[-1]["attributes"], {"exit_code": code})
                self.assertEqual(events[-2]["attributes"]["exit_code"], code)
                self.assertEqual(
                    events[2]["attributes"]["stage_id"], events[-2]["attributes"]["stage_id"]
                )
                self.assertIsNone(events[1]["attributes"]["run_id"])
                self.assertEqual(observed["callbacks"], f"exit:{code}\n".encode())
                timeline = observed["timeline"]
                self.assertLess(
                    timeline.index("recorder:prepare:start"), timeline.index("configure")
                )
                self.assertLess(
                    timeline.index("configure"), timeline.index("recorder:prepare:context")
                )
                self.assertLess(
                    timeline.index("recorder:prepare:stage-start"), timeline.index("business")
                )

    def test_configuration_failure_and_usage_do_not_invent_business_facts(self) -> None:
        for kwargs, code in [
            ({"settings": {"CONFIGURE_RC": "23"}}, 23),
            ({"no_command": True}, 2),
            ({"settings": {"EXAMPLE_CYCLE_KIND": "single"}}, 2),
        ]:
            with self.subTest(kwargs=kwargs):
                baseline, observed = self.invoke("off", **kwargs), self.invoke(**kwargs)
                self.assert_equivalent(baseline, observed)
                self.assertEqual(observed["rc"], code)
                self.assertEqual(observed["business"], b"")
                events = self.histories(observed)[0]
                self.assertEqual(
                    [e["name"] for e in events], ["execution.started", "execution.ended"]
                )
                self.assertEqual(events[-1]["attributes"], {"exit_code": code})

    def test_outer_callback_zero_keeps_the_original_failure(self) -> None:
        settings = {"BUSINESS_RC": "7", "EXAMPLE_CALLBACK_EXIT_ZERO": "1"}
        baseline, observed = self.invoke("off", settings=settings), self.invoke(settings=settings)
        self.assert_equivalent(baseline, observed)
        self.assertEqual(observed["rc"], 0)
        self.assertEqual(observed["callbacks"], b"exit:7\n")
        self.assertEqual(self.histories(observed)[0][-1]["attributes"], {"exit_code": 7})

    def test_term_uses_actual_signal_and_preserves_callback_order(self) -> None:
        baseline = self.invoke("off", signum=signal.SIGTERM)
        observed = self.invoke(signum=signal.SIGTERM)
        self.assert_equivalent(baseline, observed)
        self.assertEqual(observed["rc"], 143)
        self.assertEqual(observed["callbacks"], b"signal:TERM\nexit:143\n")
        events = self.histories(observed)[0]
        self.assertEqual(events[-1]["attributes"], {"signal": 15})
        self.assertEqual(events[-2]["name"], "stage.ended")
        self.assertEqual(events[-2]["attributes"]["signal"], 15)
        for fault in ["missing_tool", "complete_error"]:
            with self.subTest(fault=fault):
                failed = self.invoke(signum=signal.SIGTERM, fault=fault)
                self.assert_equivalent(baseline, failed, fault=True)
                self.assertTrue(
                    all(
                        e["name"] != "execution.ended"
                        for history in self.histories(failed)
                        for e in history
                    )
                )

    def test_kill_leaves_only_observed_facts(self) -> None:
        baseline = self.invoke("off", signum=signal.SIGKILL)
        observed = self.invoke(signum=signal.SIGKILL)
        self.assert_equivalent(baseline, observed)
        self.assertEqual(observed["rc"], -9)
        self.assertEqual(observed["callbacks"], b"")
        self.assertEqual(
            [e["name"] for e in self.histories(observed)[0]],
            ["execution.started", "execution.context", "stage.started", "example.progress"],
        )

    def test_fault_matrix_preserves_real_business_and_disables_later_cli(self) -> None:
        for code in ["0", "7"]:
            baseline = self.invoke("off", settings={"BUSINESS_RC": code})
            for fault in [
                "missing_tool",
                "root_incompatible",
                "short",
                "complete_error",
                "close_error",
                "tail",
                "malformed",
                "tool_signal",
            ]:
                with self.subTest(code=code, fault=fault):
                    observed = self.invoke(fault=fault, settings={"BUSINESS_RC": code})
                    self.assert_equivalent(baseline, observed, fault=True)
                    histories = self.histories(observed, partial=fault in {"short", "tail"})
                    if fault in {"missing_tool", "root_incompatible"}:
                        self.assertEqual(histories, [])
                        self.assertEqual(len(observed["cli"]), 0 if fault == "missing_tool" else 2)
                    else:
                        self.assertEqual(len(observed["cli"]), 7 if fault == "malformed" else 8)
                        self.assertEqual(
                            [e["name"] for e in histories[0]],
                            ["execution.started", "execution.context", "stage.started"]
                            + (
                                ["example.progress"]
                                if fault in {"complete_error", "close_error"}
                                else []
                            ),
                        )

    def test_real_permission_denial_preserves_business(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("permission bits cannot reject a privileged writer")
        baseline = self.invoke("off", settings={"BUSINESS_RC": "7"})
        observed = self.invoke(fault="permission", settings={"BUSINESS_RC": "7"})
        self.assert_equivalent(baseline, observed, fault=True)
        self.assertEqual(self.histories(observed), [])
        self.assertEqual(len(observed["cli"]), 2)

    def test_restarts_and_concurrent_executions_never_overwrite_history(self) -> None:
        first = self.invoke()
        paths = {p: p.read_bytes() for p in first["root"].glob("executions/*/*/*.jsonl")}
        with ThreadPoolExecutor(max_workers=4) as pool:
            later = list(pool.map(lambda _: self.invoke(shared_root=first["root"]), range(4)))
        histories = self.histories(later[-1])
        self.assertEqual(len(histories), 5)
        self.assertEqual(len({h[0]["execution_id"] for h in histories}), 5)
        self.assertTrue(all(len(h) == 6 for h in histories))
        for path, raw in paths.items():
            self.assertEqual(path.read_bytes(), raw)
        for result in later:
            self.assertEqual(
                (result["rc"], result["business"], result["stderr"]),
                (0, b"run\n", b"business stderr\n"),
            )

    def test_synthetic_cycle_values_and_quoted_text_use_actual_effective_values(self) -> None:
        message = "$(touch must-not-run); `touch must-not-run`\n中文"
        for kind in ["single", "none", "multiple"]:
            settings = {"EXAMPLE_CYCLE_KIND": kind, "EXAMPLE_MESSAGE": message}
            if kind == "single":
                settings["EXAMPLE_CYCLE"] = "2024-07-01T00:00:00+08:00"
            observed = self.invoke(settings=settings)
            events = self.histories(observed)[0]
            attrs = events[1]["attributes"]
            self.assertEqual(attrs["cycle_kind"], kind)
            self.assertEqual(attrs["cycle"], settings.get("EXAMPLE_CYCLE"))
            self.assertIsNone(attrs["run_id"])
            self.assertEqual(events[3]["attributes"]["message"], message)
            self.assertFalse((observed["work"] / "must-not-run").exists())

    def test_read_only_detached_installation_has_no_source_resource_dependency(self) -> None:
        install = self.work / "read only installation"
        for source, target in [
            (EXAMPLE, install / "examples/execution/standalone.sh"),
            (LIBRARY, install / "integrations/bash/sledra-execution.sh"),
            (self.binary, install / "bin/sledra"),
        ]:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        paths = list(install.rglob("*")) + [install]
        for path in paths:
            path.chmod(0o555 if path.is_dir() or path.name == "sledra" else 0o444)

        def restore():
            for path in paths:
                if path.is_dir():
                    path.chmod(0o755)

        self.addCleanup(restore)
        before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths if p.is_file()}
        observed = self.invoke(installation=install, filename_only=True)
        self.assertEqual((observed["rc"], observed["stderr"]), (0, b"business stderr\n"))
        self.assertEqual(len(self.histories(observed)[0]), 6)
        self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})


if __name__ == "__main__":
    unittest.main(verbosity=2)
