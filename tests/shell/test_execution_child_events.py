"""CE-03 public hooks with real Linux Go recording and independent Bash callers."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "spec/execution-v2"))
from check import stream_conflicts, validate  # noqa: E402

DIAG = b'{"ok":false,"code":"execution_observation_unavailable"}\n'
OWNER = ROOT / "integrations/bash/sledra-execution.sh"
EVENTS = ROOT / "integrations/bash/sledra-events.sh"


class ChildEventsShell(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="sledra-ce03-bin-")
        cls.addClassCleanup(cls.build.cleanup)
        override = os.environ.get("SLEDRA_TEST_BIN_DIR")
        cls.binary = Path(override) / "sledra" if override else Path(cls.build.name) / "sledra"
        if not override:
            subprocess.run(
                ["go", "build", "-buildvcs=false", "-o", str(cls.binary), "./cmd/sledra"],
                cwd=ROOT / "recorder",
                env=os.environ | {"GOPROXY": "off", "GOSUMDB": "off", "GOTOOLCHAIN": "local"},
                capture_output=True,
                check=True,
            )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="sledra-ce03-", dir=Path.home() / ".cache")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)
        self.runtime = self.work / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.records = self.work / "records"
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("SLEDRA_", "_SLEDRA_", "BASH_FUNC_"))
            and k not in {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"}
        } | {
            "SLEDRA_MODE": "execution-v2",
            "SLEDRA_BIN": str(self.binary),
            "SLEDRA_ROOT": str(self.records),
            "SLEDRA_RUNTIME_DIR": str(self.runtime),
            "OWNER": str(OWNER),
            "EVENTS": str(EVENTS),
            "WORK": str(self.work),
            "REAL_BIN": str(self.binary),
            "ECF_PASS": "synthetic-secret-not-a-credential",
        }
        self.init_root(self.records)
        self.addCleanup(self.retain)

    def retain(self):
        destination = os.environ.get("SLEDRA_CE03_EVIDENCE")
        if destination:
            target = Path(destination) / self.id().split(".")[-1]
            shutil.copytree(self.work, target, dirs_exist_ok=True)

    def init_root(self, root):
        subprocess.run(
            [
                str(self.binary),
                "execution",
                "init-root",
                "--root",
                str(root),
                "--system",
                "synthetic",
                "--deployment",
                "ce03",
                "--partition-timezone=+00:00",
            ],
            capture_output=True,
            check=True,
        )

    def shell(self, script, env=None, status=0):
        result = subprocess.run(
            ["/bin/bash", "--noprofile", "--norc", "-c", script],
            cwd=self.work,
            env=self.env | (env or {}),
            capture_output=True,
            timeout=30,
        )
        if os.environ.get("SLEDRA_CE03_EVIDENCE"):
            with (self.work / "runs.jsonl").open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "returncode": result.returncode,
                            "expected": status,
                            "stdout": result.stdout.decode("utf-8", errors="replace"),
                            "stderr": result.stderr.decode("utf-8", errors="replace"),
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
        self.assertEqual(result.returncode, status, result.stderr)
        self.assertNotIn(self.env["ECF_PASS"].encode(), result.stderr)
        return result

    def histories(self, root=None):
        histories = []
        for path in sorted((root or self.records).glob("executions/*/*/*.jsonl")):
            raw = path.read_bytes()
            self.assertEqual(stream_conflicts(raw), [])
            self.assertTrue(raw.endswith(b"\n"))
            histories.append([validate("event", line) for line in raw.splitlines()])
        return histories

    def proxy(self, body):
        path = self.work / "proxy"
        path.write_text(
            '#!/bin/bash\nprintf "%s\\n" "$3" >> "$WORK/calls"\n'
            + body
            + '\nexec "$REAL_BIN" "$@"\n'
        )
        path.chmod(0o700)
        return {"SLEDRA_BIN": str(path)}

    def calls(self):
        return (self.work / "calls").read_text().splitlines()

    def test_source_off_missing_reference_and_unattached_emit_do_no_cli_work(self):
        result = self.shell(
            r"""
set -aEeuf -o pipefail
trap ':' ERR EXIT TERM
before=$-:$SHELLOPTS:$BASHOPTS
traps=$(trap -p)
source "$EVENTS"
[[ -z ${!_SLEDRA_CE_@} ]]
sledra_event_emit data.ready --int lead 0
sledra_event_attach
source "$EVENTS"
sledra_event_attach --context ''
sledra_event_detach
[[ $-:$SHELLOPTS:$BASHOPTS == "$before" && $(trap -p) == "$traps" ]]
[[ $(export -fp) != *sledra* ]]
""",
            env={"SLEDRA_BIN": "/does/not/exist"},
        )
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        self.assertEqual(list(self.runtime.iterdir()), [])
        for mode in ("off", ""):
            result = self.shell(
                r"""
set -euo pipefail
source "$EVENTS"
sledra_event_attach --context 'not/a/reference'
export SLEDRA_MODE=execution-v2
sledra_event_attach --context 'still/invalid'
sledra_event_emit stage.started
source "$OWNER"
sledra_execution_begin --child-events
[[ -z ${SLEDRA_EVENT_CONTEXT:-} ]]
""",
                env={
                    "SLEDRA_MODE": mode,
                    "SLEDRA_EVENT_CONTEXT": "stale-reference",
                    "SLEDRA_BIN": "/missing",
                },
            )
            # The later owner begin is enabled explicitly and diagnoses missing installation.
            self.assertEqual(result.stdout, b"")
            self.assertEqual(result.stderr, DIAG)

    def test_real_inline_external_lifecycle_and_exact_typed_values(self):
        result = self.shell(r"""
set -aEeuf -o pipefail
trap ':' ERR EXIT TERM
before=$-:$SHELLOPTS:$BASHOPTS
traps=$(trap -p)
source "$OWNER"
source "$EVENTS"
sledra_execution_begin --child-events --task synthetic.initial
ref=$SLEDRA_EVENT_CONTEXT
sledra_execution_begin --child-events --task ignored
[[ $SLEDRA_EVENT_CONTEXT == "$ref" ]]
sledra_execution_context --task config --cycle-kind single --cycle 2026-10-31T08:00:00.000000001+08:00 --int members 9007199254740993
sledra_execution_context --task config --cycle-kind single --cycle 2026-10-31T00:00:00.000000001Z --float members 9007199254740993.0
sledra_stage_begin prepare
(
    sledra_execution_event forbidden.owner
    sledra_event_emit forbidden.unattached
    sledra_event_attach
    sledra_event_attach
    source "$EVENTS"
    for lead in 0 3; do sledra_event_emit data.ready --int lead "$lead"; done
    sledra_event_detach
    sledra_event_emit forbidden.detached
    sledra_event_attach --context "$SLEDRA_EVENT_CONTEXT"
    sledra_event_emit data.ready --int lead 6
)
bash -c 'source "$EVENTS"; sledra_event_attach; for lead in 9 12; do sledra_event_emit data.ready --int lead "$lead"; done'
sledra_execution_event data.typed --int count 9007199254740993 --float fraction 0.10000000000000001 --bool ready false --null absent --string literal '$(must-not-run) 中文'
sledra_stage_end --exit-code 0
sledra_execution_end --exit-code 0
sledra_execution_end --exit-code=00
sledra_stage_begin ignored
[[ $_SLEDRA_V2_STATE == closed ]]
[[ $-:$SHELLOPTS:$BASHOPTS == "$before" && $(trap -p) == "$traps" ]]
for name in "${!_SLEDRA_V2_@}" "${!_SLEDRA_CE_@}"; do
    declaration=$(declare -p "$name")
    [[ ! $declaration =~ ^declare\ -[^\ ]*x ]]
done
[[ $(export -fp) != *sledra* ]]
""")
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        events = self.histories()[0]
        self.assertEqual(len(events), 11)
        self.assertEqual(
            [e["attributes"]["lead"] for e in events if e["name"] == "data.ready"], [0, 3, 6, 9, 12]
        )
        self.assertEqual(events[-1]["name"], "execution.ended")
        attrs = next(e["attributes"] for e in events if e["name"] == "data.typed")
        self.assertEqual(attrs["count"], 9007199254740993)
        self.assertEqual(attrs["literal"], "$(must-not-run) 中文")
        state = json.loads(next(self.runtime.glob("*/state.json")).read_bytes())
        self.assertEqual(state["phase"], "closed")

    def test_rejected_events_stages_and_single_shared_diagnostic(self):
        result = self.shell(r"""
set -euo pipefail
source "$OWNER"
source "$EVENTS"
sledra_execution_begin --child-events
for name in execution.context stage.started; do
    bash -c 'source "$EVENTS"; sledra_event_attach; sledra_event_emit "$1" --string secret "$ECF_PASS"' _ "$name"
done
sledra_execution_event data.bad --int duplicate 1 --string duplicate bad
sledra_execution_event data.bad --context forged
sledra_stage_end --exit-code 0
sledra_stage_begin first
sledra_stage_begin second
sledra_stage_end --signal 0
sledra_execution_event data.good --int count 1
sledra_stage_end --exit-code 0
sledra_execution_end --exit-code 0
sledra_execution_end --exit-code 1
""")
        self.assertEqual((result.stdout, result.stderr), (b"", DIAG))
        self.assertEqual(
            [e["name"] for e in self.histories()[0]],
            ["execution.started", "stage.started", "data.good", "stage.ended", "execution.ended"],
        )
        self.assertEqual(len(list(self.runtime.glob("*/diagnostic"))), 1)

    def test_context_conflict_disables_owner_and_other_producers(self):
        result = self.shell(r"""
set -euo pipefail
source "$OWNER"
source "$EVENTS"
sledra_execution_begin --child-events
sledra_event_attach
sledra_execution_context --task first --cycle-kind none
sledra_execution_context --task second --cycle-kind none
sledra_event_emit data.after
bash -c 'source "$EVENTS"; sledra_event_attach; sledra_event_emit data.after'
sledra_execution_end --exit-code 0
[[ $_SLEDRA_V2_STATE == disabled ]]
""")
        self.assertEqual((result.stdout, result.stderr), (b"", DIAG))
        self.assertEqual(len(self.histories()[0]), 2)
        self.assertEqual(len(list(self.runtime.glob("*/disabled"))), 1)

    def test_set_a_inherited_private_environment_is_discarded(self):
        result = self.shell(r"""
set -aeuo pipefail
export _SLEDRA_V2_OWNER=$BASHPID _SLEDRA_V2_STATE=active _SLEDRA_V2_CHILD=1 _SLEDRA_V2_CE_KEY=forged
export _SLEDRA_CE_PID=$BASHPID _SLEDRA_CE_STATE=on _SLEDRA_CE_REF=forged _SLEDRA_CE_ID=forged
source "$OWNER"
source "$EVENTS"
sledra_event_emit forbidden.inherited
sledra_execution_begin --child-events
sledra_execution_event data.owner
bash -c 'source "$EVENTS"; sledra_event_emit forbidden.inherited; sledra_event_attach; sledra_event_emit data.external'
sledra_execution_end --exit-code 0
""")
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        self.assertEqual(
            [e["name"] for e in self.histories()[0]],
            ["execution.started", "data.owner", "data.external", "execution.ended"],
        )

    def test_detach_and_reference_switch_between_two_live_owners(self):
        other = self.work / "other-records"
        self.init_root(other)
        result = self.shell(
            r"""
set -euo pipefail
source "$OWNER"
source "$EVENTS"
sledra_execution_begin --child-events
first=$SLEDRA_EVENT_CONTEXT
mkfifo "$WORK/release"
SLEDRA_ROOT="$OTHER" bash -c 'source "$OWNER"; sledra_execution_begin --child-events; printf "%s\n" "$SLEDRA_EVENT_CONTEXT" > "$WORK/other-ref"; IFS= read -r release < "$WORK/release"; sledra_execution_end --exit-code 0' &
other_pid=$!
while [[ ! -s $WORK/other-ref ]]; do sleep 0.01; done
IFS= read -r second < "$WORK/other-ref"
sledra_event_attach --context "$first"
sledra_event_attach --context "$second"
sledra_event_emit data.first
sledra_event_detach
sledra_event_attach --context "$second"
sledra_event_emit data.second
sledra_event_detach
sledra_event_attach --context "$first"
sledra_event_emit data.first
printf 'release\n' > "$WORK/release"
wait "$other_pid"
sledra_execution_end --exit-code 0
""",
            env={"OTHER": str(other)},
        )
        self.assertEqual((result.stdout, result.stderr), (b"", DIAG))
        self.assertEqual(
            [e["name"] for e in self.histories()[0]],
            ["execution.started", "data.first", "data.first", "execution.ended"],
        )
        self.assertEqual(
            [e["name"] for e in self.histories(other)[0]],
            ["execution.started", "data.second", "execution.ended"],
        )
        (self.work / "release").unlink()  # Do not retain a FIFO as evidence.

    def test_lost_commit_receipt_has_one_result_and_no_reprepare(self):
        env = self.proxy(r"""
if [[ $3 == commit ]]; then
    "$REAL_BIN" "$@" >/dev/null
    kill -TERM "$BASHPID"
fi
""")
        result = self.shell(
            r"""
set -euo pipefail
source "$OWNER"
source "$EVENTS"
sledra_execution_begin --child-events
sledra_event_attach
sledra_event_emit data.once
sledra_event_emit data.twice
sledra_execution_end --exit-code 0
""",
            env=env,
        )
        self.assertEqual(result.stdout, b"")
        # Bash may report the CLI's signal in the capture shell; it must stay captured.
        self.assertEqual(result.stderr, b"")
        self.assertEqual(len(self.histories()[0]), 4)
        self.assertEqual(self.calls().count("commit"), 3)
        self.assertEqual(self.calls().count("result"), 3)
        self.assertEqual(self.calls().count("prepare"), 4)
        self.assertNotIn("disable", self.calls())

    def test_unknown_result_locks_process_across_detach_and_source(self):
        env = self.proxy(r"""
if [[ $3 == commit || $3 == result ]]; then kill -KILL "$BASHPID"; fi
""")
        result = self.shell(
            r"""
set -euo pipefail
source "$OWNER"
source "$EVENTS"
sledra_execution_begin --child-events
sledra_event_attach
sledra_event_emit data.unknown
sledra_event_detach
source "$EVENTS"
sledra_event_attach
sledra_event_emit data.after
[[ $_SLEDRA_CE_STATE == disabled ]]
""",
            env=env,
        )
        self.assertEqual((result.stdout, result.stderr), (b"", DIAG))
        self.assertEqual(len(self.histories()[0]), 1)
        self.assertEqual(self.calls().count("prepare"), 2)
        self.assertEqual(self.calls().count("commit"), 1)
        self.assertEqual(self.calls().count("result"), 1)
        self.assertEqual(self.calls().count("disable"), 1)

    def test_busy_drops_fact_and_explicit_end_uses_first_prepared(self):
        # Hold the real session flock from a child until the owner releases it.
        locker = self.work / "locker.py"
        locker.write_text(
            'import fcntl,sys\nf=open(sys.argv[1],"r+")\nfcntl.flock(f,fcntl.LOCK_EX)\nprint("locked",flush=True)\nsys.stdin.readline()\n'
        )
        env = self.proxy(":")
        result = self.shell(
            r"""
set -euo pipefail
source "$OWNER"
source "$EVENTS"
sledra_execution_begin --child-events
sledra_event_attach
lock=${_SLEDRA_V2_CE_REF#sledra-ce1:}; lock=${lock%/*}/writer.lock
coproc LOCKER { python "$WORK/locker.py" "$lock"; }
IFS= read -r ready <&"${LOCKER[0]}"
sledra_event_emit data.dropped
sledra_execution_end --exit-code 0
first=$_SLEDRA_V2_CE_END
sledra_execution_event data.closed
printf 'release\n' >&"${LOCKER[1]}"
wait "$LOCKER_PID"
sledra_execution_end --exit-code=00
[[ $_SLEDRA_V2_CE_END == "$first" && $_SLEDRA_V2_STATE == closed ]]
""",
            env=env,
        )
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        self.assertEqual(
            [e["name"] for e in self.histories()[0]], ["execution.started", "execution.ended"]
        )
        self.assertEqual(self.calls().count("prepare"), 3)
        self.assertEqual(self.calls().count("commit"), 3)
        self.assertNotIn("result", self.calls())
        self.assertNotIn("disable", self.calls())

    def test_missing_control_is_local_failure_and_diagnostic_falls_back_per_pid(self):
        result = self.shell(r"""
set -euo pipefail
source "$OWNER"
source "$EVENTS"
sledra_execution_begin --child-events
sledra_event_attach
session=${SLEDRA_EVENT_CONTEXT#sledra-ce1:}; session=${session%/*}
rm "$session/state.json"
sledra_event_emit data.missing
sledra_event_detach
source "$EVENTS"
sledra_event_attach
sledra_event_emit data.after
bash -c 'source "$EVENTS"; sledra_event_attach; sledra_event_emit data.child'
""")
        self.assertEqual((result.stdout, result.stderr), (b"", DIAG * 2))
        self.assertEqual(len(self.histories()[0]), 1)

    def test_missing_library_old_binary_bad_begin_and_no_fallback(self):
        copied = self.work / "owner.sh"
        shutil.copyfile(OWNER, copied)
        for arguments, extra in [
            ("--child-events", {"OWNER": str(copied)}),
            ("--child-events", {"SLEDRA_BIN": "/bin/true"}),
            ("--child-events", {"SLEDRA_RUNTIME_DIR": "relative"}),
            ("--child-events --child-events", {}),
            ("--child-events=true", {}),
            ("--child-events --unknown", {}),
        ]:
            with self.subTest(arguments=arguments, extra=extra):
                result = self.shell(
                    'set -euo pipefail\nsource "$OWNER"\nsledra_execution_begin '
                    + arguments
                    + r"""
[[ $_SLEDRA_V2_STATE == disabled && -z ${SLEDRA_EVENT_CONTEXT:-} ]]
sledra_execution_begin
sledra_execution_event forbidden.fallback
""",
                    env=extra | {"SLEDRA_EVENT_CONTEXT": "inherited-reference"},
                )
                self.assertEqual((result.stdout, result.stderr), (b"", DIAG))
        self.assertEqual(self.histories(), [])

    def test_xtrace_does_not_expose_internal_owner_key_or_prepared_records(self):
        result = self.shell(r"""
set -euxo pipefail
source "$OWNER"
sledra_execution_begin --child-events
sledra_execution_context --task config --cycle-kind none
sledra_execution_event data.ready --int lead 0
sledra_execution_end --exit-code 0
[[ $- == *x* ]]
""")
        self.assertEqual(result.stdout, b"")
        self.assertNotIn(b"--owner-key", result.stderr)
        self.assertNotIn(b"_ce_pending=", result.stderr)
        self.assertNotIn(b"_SLEDRA_V2_CE_KEY=", result.stderr)
        self.assertNotIn(b"execution child-events", result.stderr)
        self.assertEqual(len(self.histories()[0]), 4)

    def test_business_result_options_traps_and_callbacks_match_off_on_fault(self):
        script = r"""
set -Eeuo pipefail
source "$OWNER"
source "$EVENTS"
finish() {
    saved=$?
    sledra_execution_end --exit-code "$saved"
    printf 'callback:%s\n' "$saved"
}
trap finish EXIT
trap 'printf "err:%s\n" "$?"' ERR
sledra_execution_begin --child-events
printf 'argv:%s:%s\n' "$1" "$2"
( sledra_event_attach; sledra_event_emit data.ready; printf 'artifact\n' > "$WORK/product"; exit 7 )
printf 'unreachable\n'
"""
        # Arguments and payload remain in normal shell control flow, outside hooks.
        script = "set -- 'argument with spaces' 'literal *'\n" + script
        results = []
        for env in ({"SLEDRA_MODE": "off"}, {}, {"SLEDRA_BIN": "/missing"}):
            result = self.shell(script, env=env, status=7)
            results.append((result.stdout, (self.work / "product").read_bytes()))
        self.assertEqual(results, [results[0]] * 3)
        self.assertEqual(results[0][0], b"argv:argument with spaces:literal *\nerr:7\ncallback:7\n")

    def test_malformed_commit_receipts_use_one_readonly_confirmation(self):
        faults = [
            b"",
            b"1\x00\n",
            b"1\tcommitted\tok\n",
            b"x" * 66056,
            b"1\tcommitted\tok\t-\t-\t-\tactive\tnone\t-\n",
            b"1\tcommitted\tok\t-\t-\t0\tactive\tnone\t-\n\n",
        ]
        env = self.proxy(r"""
if [[ $3 == commit ]]; then
    "$REAL_BIN" "$@" >/dev/null
    cat "$WORK/fault"
    exit 0
fi
""")
        for fault in faults:
            with self.subTest(fault=fault[:64]):
                (self.work / "fault").write_bytes(fault)
                before = self.calls() if (self.work / "calls").exists() else []
                result = self.shell(
                    r"""
set -Eeuo pipefail
source "$OWNER"
sledra_execution_begin --child-events
sledra_execution_event data.once
sledra_execution_end --exit-code 0
[[ $_SLEDRA_V2_STATE == closed ]]
""",
                    env=env,
                )
                self.assertEqual((result.stdout, result.stderr), (b"", b""))
                calls = self.calls()[len(before) :]
                self.assertEqual(calls.count("commit"), 2)
                self.assertEqual(calls.count("result"), 2)
                self.assertEqual(calls.count("prepare"), 3)
                self.assertNotIn("disable", calls)
        self.assertTrue(all(len(events) == 3 for events in self.histories()))
        (self.work / "fault").unlink()

    def test_prepare_and_open_transport_failures_do_not_publish_or_reopen(self):
        for command in ("prepare", "open"):
            with self.subTest(command=command):
                env = self.proxy(
                    'if [[ $3 == "$FAULT_COMMAND" ]]; then\n    "$REAL_BIN" "$@" >/dev/null\n    printf "broken\\n"\n    exit 0\nfi\n'
                )
                before = self.calls() if (self.work / "calls").exists() else []
                result = self.shell(
                    r"""
set -euo pipefail
source "$OWNER"
sledra_execution_begin --child-events
sledra_execution_begin --child-events
sledra_execution_event forbidden.reopen
sledra_execution_end --exit-code 0
[[ -z ${SLEDRA_EVENT_CONTEXT:-} && $_SLEDRA_V2_STATE == disabled ]]
""",
                    env=env | {"FAULT_COMMAND": command},
                )
                self.assertEqual((result.stdout, result.stderr), (b"", DIAG))
                calls = self.calls()[len(before) :]
                self.assertEqual(calls.count("prepare"), 1)
                self.assertEqual(calls.count("open"), int(command == "open"))
                self.assertNotIn("result", calls)
                self.assertNotIn("commit", calls)
        self.assertEqual([len(h) for h in self.histories()], [1])

    def test_result_transport_failure_disables_once_and_cannot_rebind(self):
        env = self.proxy(r"""
if [[ $3 == commit ]]; then "$REAL_BIN" "$@" >/dev/null; exit 99; fi
if [[ $3 == result ]]; then printf 'malformed\000reply\n'; exit 0; fi
""")
        result = self.shell(
            r"""
set -euo pipefail
source "$OWNER"
source "$EVENTS"
sledra_execution_begin --child-events
sledra_event_attach
sledra_event_emit data.uncertain
sledra_event_detach
source "$EVENTS"
sledra_event_attach
sledra_event_emit forbidden.reopen
[[ $_SLEDRA_CE_STATE == disabled ]]
""",
            env=env,
        )
        self.assertEqual((result.stdout, result.stderr), (b"", DIAG))
        self.assertEqual(
            [e["name"] for e in self.histories()[0]], ["execution.started", "data.uncertain"]
        )
        self.assertEqual(self.calls().count("result"), 1)
        self.assertEqual(self.calls().count("disable"), 1)
        self.assertEqual(self.calls().count("commit"), 1)

    def test_attach_busy_can_be_explicitly_attempted_later(self):
        # A deterministic CLI refusal represents no admission; only the second
        # independently requested attach reaches the real recorder.
        env = self.proxy(r"""
if [[ $3 == bind && ! -e $WORK/busy-once ]]; then
    touch "$WORK/busy-once"
    printf '1\tbusy\twriter_busy\t-\t-\t2\tactive\tnone\t-\n'
    exit 21
fi
""")
        result = self.shell(
            r"""
set -euo pipefail
source "$OWNER"
source "$EVENTS"
sledra_execution_begin --child-events
sledra_event_attach
sledra_event_emit forbidden.unbound
[[ -z ${_SLEDRA_CE_REF:-} ]]
sledra_event_attach
sledra_event_emit data.attached
sledra_execution_end --exit-code 0
""",
            env=env,
        )
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        self.assertEqual(
            [e["name"] for e in self.histories()[0]],
            ["execution.started", "data.attached", "execution.ended"],
        )
        self.assertEqual(self.calls().count("bind"), 2)

    def test_invalid_unbound_reference_does_not_disable_a_valid_session(self):
        result = self.shell(r"""
set -euo pipefail
source "$OWNER"
source "$EVENTS"
sledra_execution_begin --child-events
sledra_event_attach --context malformed
sledra_event_attach --context --context extra
sledra_event_attach --context ''
sledra_event_emit forbidden.unbound
sledra_event_attach
sledra_event_emit data.attached
sledra_execution_end --exit-code 0
""")
        self.assertEqual((result.stdout, result.stderr), (b"", DIAG))
        self.assertEqual(
            [e["name"] for e in self.histories()[0]],
            ["execution.started", "data.attached", "execution.ended"],
        )
        self.assertFalse(list(self.runtime.glob("*/disabled")))

    def test_payload_boundaries_reject_only_the_bad_fact(self):
        result = self.shell(
            r"""
set -euo pipefail
source "$OWNER"
source "$EVENTS"
sledra_execution_begin --child-events
sledra_event_attach
sledra_event_emit data.literal --string option --previous-event --string multiline $'line\n中文\tend'
sledra_event_emit data.overflow --string payload "$HUGE"
sledra_event_emit data.duplicate --int key 1 --null key
sledra_event_emit execution.ended --int exit_code 0
sledra_event_emit data.final --bool ready false
sledra_execution_end --exit-code 0
""",
            env={"HUGE": "x" * 65536},
        )
        self.assertEqual((result.stdout, result.stderr), (b"", DIAG))
        events = self.histories()[0]
        self.assertEqual(
            [e["name"] for e in events],
            ["execution.started", "data.literal", "data.final", "execution.ended"],
        )
        self.assertEqual(
            events[1]["attributes"], {"option": "--previous-event", "multiline": "line\n中文\tend"}
        )

    def test_complete_example_business_is_identical_inline_external_and_standalone(self):
        example = ROOT / "examples/execution-child-events/owner.sh"
        business = ROOT / "examples/execution-child-events/business.sh"
        expected = b"".join(f"synthetic ready:{lead:03d}\n".encode() for lead in [0, 3, 6, 9, 12])
        for mode in ("inline", "external"):
            for env in ({"SLEDRA_MODE": "off"}, {}, {"SLEDRA_EVENTS_LIBRARY": "/missing-events"}):
                with self.subTest(mode=mode, env=env):
                    result = self.shell(
                        'bash "$EXAMPLE" "$CALL_MODE"',
                        env={"EXAMPLE": str(example), "CALL_MODE": mode} | env,
                    )
                    self.assertEqual(result.stdout, expected)
                    self.assertEqual(result.stderr, b"")
        result = self.shell('bash "$BUSINESS"', env={"BUSINESS": str(business)})
        self.assertEqual((result.stdout, result.stderr), (expected, b""))
        streams = self.histories()
        self.assertEqual(len(streams), 4)
        self.assertEqual(sorted(len(h) for h in streams), [3, 3, 8, 8])

    def test_parent_term_and_child_term_preserve_distinct_end_results(self):
        for who in ("parent", "child"):
            for mode in ("off", "execution-v2"):
                result = self.shell(
                    r"""
set -euo pipefail
source "$OWNER"
source "$EVENTS"
finish() {
    saved=$?
    if [[ ${signal_number:-0} == 15 ]]; then sledra_execution_end --signal 15
    else sledra_execution_end --exit-code "$saved"; fi
    printf 'callback:%s:%s\n' "$saved" "${signal_number:-0}"
}
trap finish EXIT
trap 'signal_number=15; exit 143' TERM
sledra_execution_begin --child-events
if [[ $SIGNAL_TARGET == parent ]]; then
    kill -TERM "$BASHPID"
fi
bash -c 'source "$EVENTS"; sledra_event_attach; sledra_event_emit data.child; kill -TERM "$BASHPID"'
""",
                    env={"SIGNAL_TARGET": who, "SLEDRA_MODE": mode},
                    status=143,
                )
                self.assertEqual(
                    result.stdout, f"callback:143:{15 if who == 'parent' else 0}\n".encode()
                )
                # A shell may report its signaled child; this is business stderr.
                self.assertNotIn(b"execution_observation_unavailable", result.stderr)
        endings = [h[-1]["attributes"] for h in self.histories()]
        self.assertCountEqual(endings, [{"signal": 15}, {"exit_code": 143}])

    def test_parent_kill_keeps_only_observed_facts_and_business_output(self):
        script = r"""
set -euo pipefail
source "$OWNER"
source "$EVENTS"
finish() { saved=$?; sledra_execution_end --exit-code "$saved"; printf 'callback\n'; }
trap finish EXIT
sledra_execution_begin --child-events
( sledra_event_attach; sledra_event_emit data.ready )
printf 'business-ready\n'
IFS= read -r release
"""
        outputs = []
        for extra in ({"SLEDRA_MODE": "off"}, {}, {"SLEDRA_BIN": "/missing"}):
            with subprocess.Popen(
                ["bash", "-c", script],
                cwd=self.work,
                env=self.env | extra,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            ) as process:
                ready = process.stdout.readline()
                self.assertEqual(ready, b"business-ready\n")
                process.send_signal(signal.SIGKILL)
                stdout, stderr = process.communicate(timeout=10)
                self.assertEqual(process.returncode, -signal.SIGKILL)
                outputs.append(ready + stdout)
                self.assertEqual(stderr, DIAG if "SLEDRA_BIN" in extra else b"")
        self.assertEqual(outputs, [b"business-ready\n"] * 3)
        self.assertEqual(
            [e["name"] for e in self.histories()[0]], ["execution.started", "data.ready"]
        )

    def test_missing_reference_freezes_configuration_without_handshake(self):
        env = self.proxy(":")
        result = self.shell(
            r"""
set -euo pipefail
source "$EVENTS"
sledra_event_attach
[[ ! -e $WORK/calls ]]
export SLEDRA_BIN=/missing
source "$OWNER"
sledra_execution_begin --child-events
export SLEDRA_BIN=$REAL_BIN
# The owner failed before creating an execution; use a separate real owner.
mkfifo "$WORK/release"
bash -c 'source "$OWNER"; sledra_execution_begin --child-events; printf "%s\n" "$SLEDRA_EVENT_CONTEXT" > "$WORK/live-ref"; IFS= read -r release < "$WORK/release"; sledra_execution_end --exit-code 0' &
owner_pid=$!
while [[ ! -s $WORK/live-ref ]]; do sleep 0.01; done
IFS= read -r live < "$WORK/live-ref"
sledra_event_attach --context "$live"
sledra_event_emit data.frozen
printf 'release\n' > "$WORK/release"
wait "$owner_pid"
""",
            env=env,
        )
        self.assertEqual((result.stdout, result.stderr), (b"", DIAG))
        self.assertEqual(self.calls().count("capabilities"), 1)
        self.assertEqual(self.calls().count("bind"), 1)
        self.assertEqual(
            [e["name"] for e in self.histories()[0]],
            ["execution.started", "data.frozen", "execution.ended"],
        )
        (self.work / "release").unlink()

    def test_normal_closed_bindings_remain_silent_and_cannot_append(self):
        result = self.shell(r"""
set -euo pipefail
source "$OWNER"
source "$EVENTS"
sledra_execution_begin --child-events
sledra_event_attach
sledra_execution_end --exit-code 0
sledra_event_emit forbidden.closed
sledra_event_attach
sledra_event_detach
sledra_event_attach
[[ -z ${_SLEDRA_CE_REF:-} ]]
bash -c 'source "$EVENTS"; sledra_event_attach; sledra_event_emit forbidden.closed'
""")
        self.assertEqual((result.stdout, result.stderr), (b"", b""))
        self.assertEqual(
            [e["name"] for e in self.histories()[0]], ["execution.started", "execution.ended"]
        )
        self.assertFalse(list(self.runtime.glob("*/diagnostic")))

    def test_cached_end_rejects_oversized_argv_without_repreparing(self):
        env = self.proxy(":")
        result = self.shell(
            r"""
set -euo pipefail
source "$OWNER"
sledra_execution_begin --child-events
sledra_execution_end --exit-code 0
printf -v too_many_zeroes '%0131073d' 0
sledra_execution_end --exit-code "$too_many_zeroes"
[[ $_SLEDRA_V2_STATE == closed ]]
sledra_execution_end --exit-code=00
""",
            env=env,
        )
        self.assertEqual((result.stdout, result.stderr), (b"", DIAG))
        self.assertEqual(len(self.histories()[0]), 2)
        self.assertEqual(self.calls().count("prepare"), 2)
        self.assertEqual(self.calls().count("commit"), 2)
        self.assertEqual(self.calls().count("diagnostic"), 1)

    def test_set_a_does_not_export_temporary_state_to_the_cli(self):
        env = self.proxy(r"""
for private in "${!_ce_@}" "${!_SLEDRA_CE_@}" "${!_SLEDRA_V2_@}"; do
    if [[ $private == _ce_business && $_ce_business == unchanged ]]; then continue; fi
    printf '%s\n' "$private" >> "$WORK/private-leaks"
    exit 99
done
""")
        result = self.shell(
            r"""
set -aeuo pipefail
export _ce_business=unchanged
source "$OWNER"
source "$EVENTS"
sledra_execution_begin --child-events
sledra_execution_context --task config --cycle-kind none
sledra_execution_event execution.ended
sledra_event_attach
sledra_event_emit data.producer
sledra_stage_begin first
sledra_execution_event data.owner
sledra_stage_end --exit-code 0
sledra_execution_end --exit-code 0
[[ $_SLEDRA_V2_STATE == closed ]]
[[ $(declare -p _ce_business) == 'declare -x _ce_business="unchanged"' ]]
""",
            env=env,
        )
        self.assertEqual((result.stdout, result.stderr), (b"", DIAG))
        self.assertEqual(len(self.histories()[0]), 7)
        self.assertEqual(self.calls().count("prepare"), 8)
