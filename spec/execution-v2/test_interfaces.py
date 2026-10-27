"""Check P0 shell examples against mock hook signatures, not a P1 implementation."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SPEC = Path(__file__).resolve().parent
EXAMPLE = SPEC / "interface-examples" / "standalone.sh"
FUNCTIONS = (
    "sledra_execution_begin",
    "sledra_execution_context",
    "sledra_execution_event",
    "sledra_stage_begin",
    "sledra_stage_end",
    "sledra_execution_end",
)
DIAGNOSTIC = b'{"ok":false,"code":"execution_observation_unavailable"}\n'

# This double only logs calls and returns the required 0. It does not allocate
# identities, enforce lifecycle state, call a CLI, or write observation records.
MOCK = r"""
_mock_hook() {
    if [[ ${SLEDRA_MODE:-off} == execution-v2 ]]; then
        printf '%s\0' "$#" "$@" >> "$HOOK_CALL_LOG"
        printf '%s\n' "$1" >> "$TIMELINE_LOG"
        if [[ ${MOCK_OBSERVATION_FAILURE:-0} == 1 && ${_mock_diagnosed:-0} != 1 ]]; then
            _mock_diagnosed=1
            printf '{"ok":false,"code":"execution_observation_unavailable"}\n' >&2
        fi
    fi
    return 0
}
"""


def calls(path: Path) -> list[list[str]]:
    if not path.exists():
        return []
    fields = path.read_bytes().decode().split("\0")
    assert fields.pop() == ""
    result = []
    while fields:
        length = int(fields.pop(0))
        result.append(fields[:length])
        del fields[:length]
    return result


class InterfaceExamples(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="sledra-p0-interface-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.library = self.root / "mock-execution-library.sh"
        functions = "".join(f'{name}() {{ _mock_hook {name} "$@"; }}\n' for name in FUNCTIONS)
        self.library.write_text(MOCK + functions)
        self.payload = self.root / "business.sh"
        self.payload.write_text(
            "#!/usr/bin/env bash\nset -eu\n"
            'printf "run\\n" >> "$BUSINESS_CALL_LOG"\n'
            'printf "application.payload\\n" >> "$TIMELINE_LOG"\n'
            'printf "%s\\0" "$@" > "$BUSINESS_ARGV_LOG"\n'
            'printf "business stdout\\n"\n'
            'printf "business stderr\\n" >&2\n'
            'if [[ ${BUSINESS_TERM:-0} == 1 ]]; then kill -TERM "$PPID"; fi\n'
            'exit "${BUSINESS_RC:-0}"\n'
        )
        self.payload.chmod(0o700)
        self.configure = self.root / "configure.sh"
        self.configure.write_text(
            '#!/usr/bin/env bash\nprintf "configure\\n" >> "$CONFIGURE_CALL_LOG"\n'
            'printf "application.configure\\n" >> "$TIMELINE_LOG"\n'
            'exit "${CONFIGURE_RC:-0}"\n'
        )
        self.configure.chmod(0o700)

    def invoke(self, mode: str, **settings: str) -> dict:
        work = self.root / f"run-{len(list(self.root.glob('run-*')))}"
        work.mkdir()
        env = {key: value for key, value in os.environ.items() if not key.startswith("SLEDRA_")}
        env.update(
            SLEDRA_MODE=mode,
            SLEDRA_BASH_LIBRARY=str(self.library),
            SLEDRA_BIN=str(work / "not-installed"),
            SLEDRA_ROOT=str(work / "records-must-not-be-created"),
            EXAMPLE_CONFIGURE=str(self.configure),
            EXAMPLE_CYCLE="2024-07-01T00:00:00+08:00",
            EXAMPLE_MESSAGE="forecast starts",
            EXAMPLE_CALLBACK_LOG=str(work / "callbacks"),
            HOOK_CALL_LOG=str(work / "hooks"),
            BUSINESS_CALL_LOG=str(work / "business"),
            BUSINESS_ARGV_LOG=str(work / "argv"),
            CONFIGURE_CALL_LOG=str(work / "configure"),
            TIMELINE_LOG=str(work / "timeline"),
        )
        env.update(settings)
        argv = ["space value", "", "中文", "$(printf unsafe); `printf unsafe`", "line\nbreak"]
        result = subprocess.run(
            ["bash", str(EXAMPLE), str(self.payload), *argv],
            env=env,
            capture_output=True,
            timeout=10,
            check=False,
        )

        def data(name: str) -> bytes:
            file = work / name
            return file.read_bytes() if file.exists() else b""

        self.assertFalse(Path(env["SLEDRA_ROOT"]).exists())
        self.assertFalse(list(work.glob("*.json")))
        return {
            "rc": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
            "business": data("business"),
            "argv": data("argv"),
            "callbacks": data("callbacks"),
            "configure": data("configure"),
            "hooks": calls(work / "hooks"),
            "timeline": data("timeline").decode().splitlines(),
            "expected_argv": b"\0".join(arg.encode() for arg in argv) + b"\0",
        }

    def assert_business_equal(self, baseline: dict, observed: dict, *, diagnostic=False) -> None:
        for field in ("rc", "stdout", "business", "argv", "callbacks", "configure"):
            self.assertEqual(observed[field], baseline[field], field)
        stderr = observed["stderr"]
        if diagnostic:
            self.assertEqual(stderr.count(DIAGNOSTIC), 1)
            stderr = stderr.replace(DIAGNOSTIC, b"", 1)
        self.assertEqual(stderr, baseline["stderr"])

    def test_normal_and_observation_failure_keep_business_result(self) -> None:
        for code in ("0", "7"):
            with self.subTest(code=code):
                baseline = self.invoke("off", BUSINESS_RC=code)
                for failed in ("0", "1"):
                    observed = self.invoke(
                        "execution-v2", BUSINESS_RC=code, MOCK_OBSERVATION_FAILURE=failed
                    )
                    self.assert_business_equal(baseline, observed, diagnostic=failed == "1")
                    self.assertEqual(observed["rc"], int(code))
                    self.assertEqual(observed["business"], b"run\n")
                    self.assertEqual(observed["argv"], observed["expected_argv"])
                    self.assertEqual(observed["configure"], b"configure\n")
                    timeline = observed["timeline"]
                    self.assertLess(
                        timeline.index("sledra_execution_begin"),
                        timeline.index("application.configure"),
                    )
                    self.assertLess(
                        timeline.index("application.configure"),
                        timeline.index("sledra_execution_context"),
                    )
                    self.assertLess(
                        timeline.index("sledra_stage_begin"),
                        timeline.index("application.payload"),
                    )
                    stage_ends = [h for h in observed["hooks"] if h[0] == "sledra_stage_end"]
                    expected = [["sledra_stage_end", "--exit-code", "0"]] if code == "0" else []
                    self.assertEqual(stage_ends, expected)
                    self.assertEqual(
                        observed["hooks"][0],
                        ["sledra_execution_begin", "--task", "example.forecast"],
                    )
                    self.assertEqual(
                        observed["hooks"][-1], ["sledra_execution_end", "--exit-code", code]
                    )

    def test_failed_configuration_has_no_context_or_payload(self) -> None:
        baseline = self.invoke("off", CONFIGURE_RC="23")
        observed = self.invoke("execution-v2", CONFIGURE_RC="23")
        self.assert_business_equal(baseline, observed)
        self.assertEqual(observed["rc"], 23)
        self.assertEqual(observed["business"], b"")
        self.assertEqual(
            observed["hooks"],
            [
                ["sledra_execution_begin", "--task", "example.forecast"],
                ["sledra_execution_end", "--exit-code", "23"],
            ],
        )

    def test_term_keeps_signal_fact_and_callback_policy(self) -> None:
        baseline = self.invoke("off", BUSINESS_TERM="1")
        observed = self.invoke("execution-v2", BUSINESS_TERM="1")
        self.assert_business_equal(baseline, observed)
        self.assertEqual(observed["rc"], 143)
        self.assertEqual(observed["business"], b"run\n")
        self.assertEqual(observed["callbacks"], b"signal:TERM\nexit:143\n")
        endings = [call for call in observed["hooks"] if call[0] == "sledra_execution_end"]
        self.assertEqual(endings, [["sledra_execution_end", "--signal", "15"]])

    def test_typed_values_preserve_quoted_text_without_evaluation(self) -> None:
        marker = self.root / "must-not-exist"
        message = f'$(touch "{marker}"); `touch "{marker}"`\n中文'
        observed = self.invoke("execution-v2", EXAMPLE_MESSAGE=message)
        self.assertEqual(observed["rc"], 0, observed["stderr"])
        self.assertFalse(marker.exists())
        events = [call for call in observed["hooks"] if call[0] == "sledra_execution_event"]
        self.assertEqual(
            events,
            [
                [
                    "sledra_execution_event",
                    "forecast.progress",
                    "--float",
                    "fraction",
                    "0.125",
                    "--string",
                    "message",
                    message,
                ]
            ],
        )
        context = [call for call in observed["hooks"] if call[0] == "sledra_execution_context"]
        self.assertEqual(
            context,
            [
                [
                    "sledra_execution_context",
                    "--task",
                    "example.forecast",
                    "--cycle-kind",
                    "single",
                    "--cycle",
                    "2024-07-01T00:00:00+08:00",
                    "--int",
                    "resolution_km",
                    "25",
                    "--string",
                    "member",
                    "01",
                ]
            ],
        )
        endings = [call for call in observed["hooks"] if call[0] == "sledra_execution_end"]
        self.assertEqual(endings, [["sledra_execution_end", "--exit-code", "0"]] * 2)

    def test_bash_syntax_and_interface_names_match_documentation(self) -> None:
        subprocess.run(["bash", "-n", str(EXAMPLE)], check=True, capture_output=True)
        documentation = (SPEC / "interfaces.md").read_text()
        for function in FUNCTIONS:
            self.assertIn(f"| `{function}` |", documentation)
        source = EXAMPLE.read_text()
        for forbidden in (
            "execution_id",
            "event_id",
            "stage_id",
            "token",
            "json",
            "python",
            "eval",
            "sledra_begin_run",
        ):
            self.assertNotIn(forbidden, source.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
