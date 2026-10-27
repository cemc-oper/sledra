"""Real Bash/Go lifecycle tests, isolated from old launchers and business roots."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "integrations/bash/sledra-execution.sh"
sys.path.insert(0, str(ROOT / "spec/execution-v2"))
from check import execution_path, stream_conflicts, validate  # noqa: E402

DIAG = b'{"ok":false,"code":"execution_observation_unavailable"}\n'


class ExecutionOwner(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.build = tempfile.TemporaryDirectory(prefix="sledra lifecycle binary ")
        cls.addClassCleanup(cls.build.cleanup)
        cls.binary = Path(cls.build.name) / "sledra"
        subprocess.run(
            ["go", "build", "-o", str(cls.binary), "./cmd/sledra"],
            cwd=ROOT / "recorder",
            env=os.environ | {"GOPROXY": "off", "GOSUMDB": "off", "GOTOOLCHAIN": "local"},
            capture_output=True,
            check=True,
        )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="sledra owner ")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.root = self.work / "records"
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("SLEDRA_", "_SLEDRA_V2_")) and k not in {"BASH_ENV", "ENV"}
        } | {
            "SLEDRA_MODE": "execution-v2",
            "SLEDRA_ROOT": str(self.root),
            "SLEDRA_BIN": str(self.binary),
            "LIBRARY": str(LIBRARY),
            # Prove unrelated legacy configuration/context/Run input is ignored.
            "SLEDRA_SPEC_DIR": "invalid-relative-path",
            "SLEDRA_CONFIG": "/missing-producer",
            "SLEDRA_EXECUTION_ID": "inherited-public-identity",
            "SLEDRA_RUN_ID": "stale-run",
            "ECF_NAME": "/must/not/infer",
        }
        self.cli(
            "init-root",
            "--root",
            str(self.root),
            "--system",
            "synthetic",
            "--deployment",
            "local",
            "--partition-timezone=+08:00",
        )

    def cli(self, *args: str, status: int = 0, data: bytes = b"") -> subprocess.CompletedProcess:
        result = subprocess.run(
            [str(self.binary), "execution", *args],
            cwd=self.work,
            env=self.env,
            input=data,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, status, result.stderr)
        if status:
            self.assertEqual(result.stdout, b"")
            self.assertLessEqual(len(result.stderr), 256)
            self.assertEqual(result.stderr.count(b"\n"), 1)
        return result

    def shell(self, source: str, *, env: dict | None = None, status: int = 0):
        result = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-c", source],
            cwd=self.work,
            env=self.env | (env or {}),
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, status, result.stderr)
        return result

    def histories(self) -> list[list[dict]]:
        config = validate("root", (self.root / "sledra.json").read_bytes())
        paths = sorted(self.root.glob("executions/*/*/*.jsonl"))
        histories = []
        for path in paths:
            raw = path.read_bytes()
            self.assertTrue(raw.endswith(b"\n"))
            self.assertEqual(stream_conflicts(raw), [])
            events = [validate("event", line) for line in raw.splitlines()]
            self.assertEqual(
                str(path.relative_to(self.root)),
                execution_path(events[0]["execution_id"], events[0]["occurred_at"], config),
            )
            self.assertTrue(all(e["execution_id"] == events[0]["execution_id"] for e in events))
            histories.append(events)
        self.assertEqual(
            {p for p in self.root.rglob("*") if p.is_file()},
            set(paths) | {self.root / "sledra.json"},
        )
        return histories

    def test_source_is_definition_only_and_preserves_options_traps(self) -> None:
        result = self.shell(r"""
set -aEeuf -o pipefail
IFS=$'\n_'
trap 'printf "unexpected ERR\n" >&2' ERR
trap ':' EXIT
before_options=$(set +o)
before_traps=$(trap -p)
before_flags=$-:$SHELLOPTS:$BASHOPTS
before_ifs=$IFS
source "$LIBRARY"
[[ $(set +o) == "$before_options" && $(trap -p) == "$before_traps" ]]
[[ $-:$SHELLOPTS:$BASHOPTS == "$before_flags" ]]
[[ -z ${!_SLEDRA_V2_@} ]]
[[ $(export -fp) != *sledra* ]]
sledra_execution_begin
source "$LIBRARY"
sledra_execution_begin --task ignored
sledra_execution_context --task config --cycle-kind none
sledra_execution_end --exit-code 0
[[ $(set +o) == "$before_options" && $(trap -p) == "$before_traps" ]]
[[ $-:$SHELLOPTS:$BASHOPTS == "$before_flags" ]]
[[ $_SLEDRA_V2_STATE == closed ]]
[[ $IFS == "$before_ifs" ]]
[[ $(export -fp) != *sledra* ]]
for name in "${!_SLEDRA_V2_@}"; do
    value=$(declare -p "$name")
    [[ ! $value =~ ^declare\ -[^\ ]*x ]]
done
""")
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        self.assertEqual(len(self.histories()[0]), 3)

    def test_context_is_exact_once_and_uses_typed_effective_values(self) -> None:
        message = '中文\n$HOME $(touch must-not-run) `literal` "quote" \\ tab\t'
        result = self.shell(
            r"""
set -euo pipefail
source "$LIBRARY"
sledra_execution_begin --task entry --engine-kind synthetic --engine-node /alias --engine-alias true
sledra_execution_context --task final --cycle-kind single --cycle 2026-10-27T08:00:00.000000001+08:00 \
    --int count 9007199254740993 --float fraction 0.10000000000000001 --bool enabled false \
    --null absent --string empty '' --string message "$MESSAGE"
first=$_SLEDRA_V2_CONTEXT
source "$LIBRARY"
sledra_execution_context --task final --cycle-kind single --cycle 2026-10-27T00:00:00.000000001Z \
    --float count 9007199254740993.0 --float fraction 0.100000000000000010 --bool enabled false \
    --null absent --string empty '' --string message "$MESSAGE"
[[ $_SLEDRA_V2_CONTEXT == "$first" && $_SLEDRA_V2_STATE == active ]]
sledra_execution_end --signal 15
sledra_execution_end --signal 15
source "$LIBRARY"
sledra_execution_begin
sledra_execution_context --task ignored --cycle-kind none
""",
            env={"MESSAGE": message},
        )
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        events = self.histories()[0]
        self.assertEqual(
            [e["name"] for e in events],
            ["execution.started", "execution.context", "execution.ended"],
        )
        self.assertEqual(events[0]["attributes"]["engine"]["node"], "/alias")
        attrs = events[1]["attributes"]
        self.assertIsNone(attrs["run_id"])
        self.assertEqual(attrs["parameters"]["count"], 9007199254740993)
        self.assertEqual(attrs["parameters"]["message"], message)
        self.assertEqual(events[2]["attributes"], {"signal": 15})
        self.assertFalse((self.work / "must-not-run").exists())

    def test_configuration_failure_preserves_business_callback_result(self) -> None:
        script = r"""
set -Eeuo pipefail
source "$LIBRARY"
finish() {
    saved=$?
    sledra_execution_end --exit-code "$saved"
    sledra_execution_end --exit-code "$saved"
    printf 'abort callback %s\n' "$saved"
    exit 0
}
trap finish EXIT
sledra_execution_begin
printf 'configuration attempted\n'
(exit 7)
printf 'business must not execute\n'
"""
        baseline = self.shell(script, env={"SLEDRA_MODE": "off"})
        observed = self.shell(script)
        self.assertEqual((observed.stdout, observed.stderr), (baseline.stdout, baseline.stderr))
        self.assertEqual(observed.stdout, b"configuration attempted\nabort callback 7\n")
        events = self.histories()[0]
        self.assertEqual([e["name"] for e in events], ["execution.started", "execution.ended"])
        self.assertEqual(events[1]["attributes"], {"exit_code": 7})

    def test_no_begin_and_subshell_hooks_never_create_or_write(self) -> None:
        result = self.shell(r"""
set -euo pipefail
source "$LIBRARY"
sledra_execution_context --task early --cycle-kind none
sledra_execution_end --exit-code 0
(sledra_execution_begin)
[[ -z $(sledra_execution_begin) ]]
sledra_execution_begin | :
sledra_execution_begin
(sledra_execution_context --task child --cycle-kind none; sledra_execution_end --exit-code 7)
[[ -z $(sledra_execution_end --exit-code 8) ]]
sledra_execution_context --task child --cycle-kind none | :
[[ -z ${_SLEDRA_V2_CONTEXT:-} && $_SLEDRA_V2_STATE == active ]]
sledra_execution_end --exit-code 0
""")
        self.assertEqual((result.stdout, result.stderr), (b"", DIAG))
        self.assertEqual(len(self.histories()[0]), 2)

    def test_real_restarts_and_exec_discard_exported_identity(self) -> None:
        child = self.work / "child.sh"
        child.write_text("""#!/bin/bash
set -euo pipefail
source "$LIBRARY"
sledra_execution_begin
[[ $_SLEDRA_V2_ID != "$OLD_ID" ]]
[[ $BASHPID == "$OLD_PID" ]]
sledra_execution_end --exit-code 0
""")
        result = self.shell(
            r"""
set -euo pipefail
source "$LIBRARY"
sledra_execution_begin
sledra_execution_end --exit-code 0
export OLD_ID=$_SLEDRA_V2_ID OLD_PID=$BASHPID
for name in ${!_SLEDRA_V2_@}; do export "$name"; done
exec /bin/bash "$CHILD"
""",
            env={"CHILD": str(child)},
        )
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        once = {p: p.read_bytes() for p in self.root.glob("executions/*/*/*.jsonl")}
        for _ in range(2):
            result = self.shell(
                'source "$LIBRARY"; sledra_execution_begin; sledra_execution_end --exit-code 0'
            )
            self.assertEqual((result.stdout, result.stderr), (b"", b""))
        histories = self.histories()
        self.assertEqual(len({h[0]["execution_id"] for h in histories}), 4)
        self.assertTrue(all(len(h) == 2 for h in histories))
        for path, raw in once.items():
            self.assertEqual(path.read_bytes(), raw)

    def test_context_conflict_disables_and_end_conflict_stays_closed(self) -> None:
        for script, state, count in [
            (
                """sledra_execution_context --task a --cycle-kind none
sledra_execution_context --task b --cycle-kind none
sledra_execution_context --task a --cycle-kind none
sledra_execution_end --exit-code 0""",
                "disabled",
                2,
            ),
            (
                """sledra_execution_end --exit-code 7
sledra_execution_end --exit-code 7
sledra_execution_end --exit-code 8
sledra_execution_end --exit-code 7""",
                "closed",
                2,
            ),
        ]:
            with self.subTest(state=state):
                result = self.shell(
                    'set -euo pipefail\nsource "$LIBRARY"\nsledra_execution_begin\n'
                    + script
                    + f"\n[[ $_SLEDRA_V2_STATE == {state} ]]"
                )
                self.assertEqual((result.stdout, result.stderr), (b"", DIAG))
                self.assertTrue(all(len(h) == count for h in self.histories()))

    def test_prewrite_rejections_keep_active_and_first_begin_failure_locks(self) -> None:
        result = self.shell(r"""
set -euo pipefail
source "$LIBRARY"
sledra_execution_begin
sledra_execution_context --task a --cycle-kind none --run-id stale
sledra_execution_context --task a --cycle-kind none --int count 01
sledra_execution_context --task a --cycle-kind single
sledra_execution_context --task a --cycle-kind none --string key a --null key
sledra_execution_context --task a --cycle-kind none --previous-event '{}'
sledra_execution_end --exit-code 256
sledra_execution_end --signal 0
[[ $_SLEDRA_V2_STATE == active && -z ${_SLEDRA_V2_CONTEXT:-} ]]
sledra_execution_context --task valid --cycle-kind multiple
sledra_execution_end --exit-code 0
""")
        self.assertEqual((result.stdout, result.stderr), (b"", DIAG))
        self.assertEqual(len(self.histories()[0]), 3)
        result = self.shell(r"""
set -euo pipefail
source "$LIBRARY"
sledra_execution_begin --task
source "$LIBRARY"
sledra_execution_begin --task valid
sledra_execution_end --exit-code 0
[[ $_SLEDRA_V2_STATE == disabled && -z ${_SLEDRA_V2_ID:-} ]]
""")
        self.assertEqual((result.stdout, result.stderr), (b"", DIAG))
        self.assertEqual(len(self.histories()), 1)

    def test_off_ambiguous_tool_and_frozen_settings(self) -> None:
        for mode in ["", "off", "invalid", "execution-v2"]:
            with self.subTest(mode=mode):
                result = self.shell(
                    r"""
set -euo pipefail
source "$LIBRARY"
sledra_execution_begin
SLEDRA_MODE=execution-v2 SLEDRA_ENABLED=0
source "$LIBRARY"
sledra_execution_begin
sledra_execution_end --exit-code 0
""",
                    env={"SLEDRA_MODE": mode, "SLEDRA_ENABLED": "1"},
                )
                self.assertEqual(result.stdout, b"")
                self.assertEqual(result.stderr, b"" if mode in {"", "off"} else DIAG)
        self.assertEqual(self.histories(), [])
        result = self.shell(
            'source "$LIBRARY"; sledra_execution_begin; SLEDRA_BIN=/missing; sledra_execution_begin; sledra_execution_end --exit-code 0',
            env={"SLEDRA_BIN": str(self.work / "missing")},
        )
        self.assertEqual((result.stdout, result.stderr), (b"", DIAG))
        result = self.shell(r"""
set -euo pipefail
source "$LIBRARY"
sledra_execution_begin
SLEDRA_MODE=off SLEDRA_BIN=/missing SLEDRA_ROOT=/missing
sledra_execution_context --task config --cycle-kind none
sledra_execution_end --exit-code 0
""")
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        self.assertEqual(len(self.histories()[0]), 3)

    def fault_binary(self, body: str) -> Path:
        wrapper = self.work / "fault-recorder"
        wrapper.write_text(
            """#!/bin/bash
printf '%s %s %s\n' "$1" "${2:-}" "${3:-}" >> "$CALL_LOG"
"""
            + body
            + '\nexec "$REAL_SLEDRA" "$@"\n'
        )
        wrapper.chmod(0o700)
        return wrapper

    def test_uncertain_and_short_writes_disable_across_cli_calls(self) -> None:
        for fault in ["short", "complete", "reject", "missing", "tail"]:
            with self.subTest(fault=fault):
                log = self.work / f"{fault}.calls"
                wrapper = self.fault_binary(r"""
if [[ $2 == append && $* != *--create* ]]; then
    case $FAULT in
        short)
            files=("$SLEDRA_ROOT"/executions/*/*/*.jsonl)
            IFS= read -r -n 7 prefix
            printf '%s' "$prefix" >> "${files[0]}"
            exit 3 ;;
        complete) "$REAL_SLEDRA" "$@"; exit 3 ;;
        reject) exit 2 ;;
    esac
fi
""")
                # Each fault uses a separate initialized root.
                case_root = self.work / f"root-{fault}"
                self.cli(
                    "init-root",
                    "--root",
                    str(case_root),
                    "--system",
                    "synthetic",
                    "--deployment",
                    "local",
                    "--partition-timezone",
                    "+00:00",
                )
                result = self.shell(
                    r"""
set -euo pipefail
source "$LIBRARY"
sledra_execution_begin
if [[ $FAULT == missing || $FAULT == tail ]]; then
    files=("$SLEDRA_ROOT"/executions/*/*/*.jsonl)
    if [[ $FAULT == missing ]]; then /bin/rm "${files[0]}"; else printf partial >> "${files[0]}"; fi
fi
sledra_execution_context --task first --cycle-kind none
[[ $_SLEDRA_V2_STATE == disabled ]]
sledra_execution_context --task later --cycle-kind none
sledra_execution_end --exit-code 0
source "$LIBRARY"
sledra_execution_begin
printf 'business once\n'
""",
                    env={
                        "SLEDRA_BIN": str(wrapper),
                        "REAL_SLEDRA": str(self.binary),
                        "CALL_LOG": str(log),
                        "FAULT": fault,
                        "SLEDRA_ROOT": str(case_root),
                    },
                )
                self.assertEqual((result.stdout, result.stderr), (b"business once\n", DIAG))
                calls = log.read_text().splitlines()
                self.assertEqual(len(calls), 4, calls)
                self.assertEqual(sum("append" in c for c in calls), 2)
                files = list(case_root.glob("executions/*/*/*.jsonl"))
                if fault == "missing":
                    self.assertEqual(files, [])
                else:
                    raw = files[0].read_bytes()
                    if fault == "short":
                        self.assertTrue(raw.endswith(b'{"attri'))
                        self.assertEqual(raw.count(b"\n"), 1)
                    elif fault == "tail":
                        self.assertTrue(raw.endswith(b"partial"))
                    else:
                        events = [validate("event", line) for line in raw.splitlines()]
                        self.assertEqual(len(events), 2 if fault == "complete" else 1)

    def test_malformed_receipts_disable_before_append(self) -> None:
        for fault in [
            "no_lf",
            "extra_line",
            "wrong_id",
            "empty_aux",
            "tab",
            "nul",
            "oversized",
            "stderr",
            "signal",
        ]:
            with self.subTest(fault=fault):
                log = self.work / f"receipt-{fault}.calls"
                wrapper = self.fault_binary(r"""
if [[ $2 == prepare ]]; then
    case $FAULT in
        no_lf) printf '01234567-89ab-4cde-8fab-0123456789ab\t-\t{}'; exit 0 ;;
        extra_line) "$REAL_SLEDRA" "$@"; printf '\n'; exit 0 ;;
        wrong_id) printf 'bad\t-\t{}\n'; exit 0 ;;
        empty_aux) printf '01234567-89ab-4cde-8fab-0123456789ab\t\t{}\n'; exit 0 ;;
        tab) printf '01234567-89ab-4cde-8fab-0123456789ab\t-\t{}\t\n'; exit 0 ;;
        nul) "$REAL_SLEDRA" "$@"; printf '\0'; exit 0 ;;
        oversized) printf '%070000d' 0; exit 0 ;;
        stderr) printf 'secret raw input\n' >&2; exit 73 ;;
        signal) kill -KILL "$BASHPID" ;;
    esac
fi
""")
                result = self.shell(
                    r"""
set -euo pipefail
source "$LIBRARY"
sledra_execution_begin
source "$LIBRARY"
sledra_execution_begin
sledra_execution_end --exit-code 0
[[ $_SLEDRA_V2_STATE == disabled ]]
""",
                    env={
                        "SLEDRA_BIN": str(wrapper),
                        "REAL_SLEDRA": str(self.binary),
                        "CALL_LOG": str(log),
                        "FAULT": fault,
                    },
                )
                self.assertEqual((result.stdout, result.stderr), (b"", DIAG))
                self.assertEqual(log.read_text().splitlines(), ["execution prepare start"])
                self.assertEqual(self.histories(), [])

    def test_no_python_or_path_lookup_and_cli_statuses(self) -> None:
        empty_path = self.work / "empty-path"
        empty_path.mkdir()
        result = self.shell(
            'set -euo pipefail; source "$LIBRARY"; sledra_execution_begin; sledra_execution_context --task t --cycle-kind none; sledra_execution_end --exit-code 0',
            env={"PATH": str(empty_path)},
        )
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        self.assertEqual(len(self.histories()[0]), 3)
        self.cli("prepare", "start", "--unknown", "secret input", status=2)
        receipt = self.cli("prepare", "start").stdout.decode()
        identity, entry, event = receipt.rstrip("\n").split("\t")
        self.cli(
            "append",
            "--root",
            str(self.work / "missing"),
            "--execution-id",
            identity,
            "--entry-time",
            entry,
            "--create",
            data=event.encode() + b"\n",
            status=3,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
