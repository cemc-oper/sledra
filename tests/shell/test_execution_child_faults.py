"""CE-04: controlled real Bash/Go processes, persistent raw bytes and operation counts."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "spec/execution-v2"))
from check import stream_conflicts, validate  # noqa: E402

sys.path.insert(0, str(ROOT / "analysis/src"))
from sledra_analysis.execution.reader import Fact, FileReader, ReadIssue  # noqa: E402

_contract_loader = importlib.util.spec_from_file_location(
    "ce04_contract", ROOT / "spec/execution-child-events/ce_contract.py"
)
contract = importlib.util.module_from_spec(_contract_loader)
_contract_loader.loader.exec_module(contract)

DIAG = b'{"ok":false,"code":"execution_observation_unavailable"}\n'
CONTROL = r"""
set -Eeuo pipefail
source "$OWNER_LIBRARY"
source "$EVENT_LIBRARY"
if [[ $CE04_ACTOR == owner* ]]; then
    sledra_execution_begin --child-events --task synthetic.ce04
    printf 'READY|%s|%s|%s\n' "${SLEDRA_EVENT_CONTEXT:--}" "${_SLEDRA_V2_ID:--}" "$_SLEDRA_V2_STATE"
else
    sledra_event_attach
    printf 'READY|%s|%s|%s\n' "$SLEDRA_EVENT_CONTEXT" "${_SLEDRA_CE_ID:--}" "$_SLEDRA_CE_STATE"
fi
while IFS='|' read -r action pause faults iomode drop kind target; do
    export CE04_PAUSE=${pause#-} CE04_FAULTS=${faults#-} CE04_IO_FAULT=${iomode#-}
    export CE04_DROP_RECEIPT=$drop CE04_KIND=${kind#-} CE04_COMMAND=${target#-}
    case $action in
        emit) sledra_event_emit synthetic.ready --int lead 3 ;;
        invalid) sledra_event_emit execution.ended --string secret "$ECF_PASS" ;;
        invalid-end) sledra_execution_end --signal 0 ;;
        stderr-close) exec 2>&- ;;
        event) sledra_execution_event synthetic.owner --int count 9007199254740993 ;;
        context) sledra_execution_context --task synthetic.ce04 --cycle-kind none ;;
        stage-start) sledra_stage_begin work ;;
        stage-end) sledra_stage_end --exit-code 0 ;;
        end) sledra_execution_end --exit-code 0 ;;
        conflicting-end) sledra_execution_end --exit-code 7 ;;
        quit) break ;;
        *) exit 90 ;;
    esac
    printf 'DONE|%s|%s\n' "$action" "${_SLEDRA_V2_STATE:-${_SLEDRA_CE_STATE:-none}}"
done
"""


class Gate:
    """One explicit ready/release handshake; no timing sleep decides an interleaving."""

    def __init__(self, case, name="gate.sock"):
        self.path = case.path / name
        self.server = socket.socket(socket.AF_UNIX)
        self.server.bind(str(self.path))
        self.server.listen(4)
        self.server.settimeout(15)
        self.connection = None
        case.test.addCleanup(self.close)

    def wait(self):
        self.connection, _ = self.server.accept()
        self.connection.settimeout(15)
        line = b""
        while not line.endswith(b"\n"):
            line += self.connection.recv(4096)
        self.event = json.loads(line)
        return self.event

    def release(self):
        self.connection.sendall(b"1")
        self.connection.close()
        self.connection = None

    def kill(self):
        os.kill(self.event["pid"], signal.SIGKILL)
        self.connection.close()
        self.connection = None

    def close(self):
        if self.connection:
            self.connection.close()
        self.server.close()
        self.path.unlink(missing_ok=True)


class Actor:
    def __init__(self, case, name, *, owner=None, env=None, script=CONTROL):
        self.case, self.name = case, name
        self.errors = (case.path / f"{name}.stderr").open("wb")
        environment = case.env | {"CE04_ACTOR": name} | (env or {})
        if owner:
            environment["SLEDRA_EVENT_CONTEXT"] = owner.reference
        self.process = subprocess.Popen(
            ["bash", "--noprofile", "--norc", "-c", script],
            env=environment,
            cwd=case.path,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.errors,
            start_new_session=True,
        )
        case.test.addCleanup(self.close)
        self.lines = []

    def read(self):
        ready, _, _ = select.select([self.process.stdout], [], [], 15)
        if not ready:
            raise AssertionError(f"actor timeout: {self.name}; trace={self.case.trace()[-8:]}")
        raw = self.process.stdout.readline()
        if not raw:
            raise AssertionError(f"actor exited: {self.name}")
        self.lines.append(raw.decode())
        (self.case.path / f"{self.name}.stdout").write_text("".join(self.lines))
        return raw.decode().rstrip("\n").split("|")

    def ready(self):
        line = self.read()
        self.case.test.assertEqual(line[0], "READY")
        self.reference, self.execution, self.state = line[1:]
        return self

    def send(self, action, *, pause="-", faults="-", io="-", drop="0", kind="-", target="-"):
        fields = [action, pause, faults, io, drop, kind, target]
        self.process.stdin.write(("|".join(fields) + "\n").encode())
        self.process.stdin.flush()

    def done(self, action):
        line = self.read()
        self.case.test.assertEqual(line[:2], ["DONE", action])
        return line[2]

    def call(self, action, **kwargs):
        self.send(action, **kwargs)
        return self.done(action)

    def close(self):
        if self.process.poll() is None:
            try:
                self.send("quit")
                self.process.wait(timeout=2)
            except (BrokenPipeError, subprocess.TimeoutExpired):
                os.killpg(self.process.pid, signal.SIGKILL)
                self.process.wait(timeout=5)
        self.process.stdin.close()
        self.process.stdout.close()
        self.errors.close()


class Case:
    def __init__(self, test, name):
        self.test = test
        self.path = test.work / name
        self.path.mkdir(mode=0o700)
        self.runtime = self.path / "runtime"
        self.runtime.mkdir(mode=0o700)
        self.records = self.path / "records"
        test.init_root(self.records)
        self.env = test.env | {
            "SLEDRA_ROOT": str(self.records),
            "SLEDRA_RUNTIME_DIR": str(self.runtime),
            "CE04_TRACE": str(self.path / "trace.jsonl"),
            "CE04_SOCKET": str(self.path / "gate.sock"),
        }

    def owner(self, **kwargs):
        return Actor(self, "owner", **kwargs).ready()

    def producer(self, owner, name="p0", **kwargs):
        return Actor(self, name, owner=owner, **kwargs).ready()

    def trace(self, *, actor=None, point=None, command=None, kind=None):
        path = self.path / "trace.jsonl"
        rows = (
            [json.loads(line) for line in path.read_bytes().splitlines()] if path.exists() else []
        )
        return [
            r
            for r in rows
            if (actor is None or r["actor"] == actor)
            and (point is None or r["point"] == point)
            and (command is None or r["command"] == command)
            and (kind is None or r.get("kind") == kind)
        ]

    def record(self):
        paths = list(self.records.glob("executions/*/*/*.jsonl"))
        self.test.assertLessEqual(len(paths), 1)
        return paths[0].read_bytes() if paths else b""

    def events(self):
        raw = self.record()
        self.test.assertEqual(stream_conflicts(raw), [])
        events = [validate("event", line) for line in raw.splitlines()]
        with FileReader(self.records) as reader:
            items = list(reader.iter_events(None, None))
        self.test.assertFalse([item for item in items if isinstance(item, ReadIssue)])
        self.test.assertEqual([item.data for item in items if isinstance(item, Fact)], events)
        return events

    def session(self):
        paths = list(self.runtime.glob("*/writer.lock"))
        self.test.assertEqual(len(paths), 1)
        return paths[0].parent

    def state(self):
        return json.loads((self.session() / "state.json").read_bytes())

    def direct(self, owner, command, *, prepared=None, env=None, **opts):
        args = [
            str(self.test.proxy),
            "execution",
            "child-events",
            command,
            "--context",
            owner.reference,
        ]
        for key, value in opts.items():
            args.extend(["--" + key.replace("_", "-"), str(value)])
        result = subprocess.run(
            args,
            env=self.env | {"CE04_ACTOR": "direct"} | (env or {}),
            input=prepared or b"",
            capture_output=True,
            timeout=15,
        )
        with (self.path / "direct-results.jsonl").open("ab") as stream:
            stream.write(
                json.dumps(
                    {
                        "command": command,
                        "status": result.returncode,
                        "stdout": result.stdout.decode(),
                        "stderr": result.stderr.decode(),
                    }
                ).encode()
                + b"\n"
            )
        return result

    def assert_one_fact(self, actor, *, writes=1):
        test = self.test
        prepared = self.trace(actor=actor, point="prepared", kind="event")
        test.assertEqual(len(prepared), 1)
        test.assertEqual(len(self.trace(actor=actor, point="call", command="commit")), 1)
        test.assertEqual(len(self.trace(actor=actor, point="write.syscall")), writes)
        return prepared[0]["record"].encode()

    def assert_stopped(self, owner, *producers):
        before = self.record()
        count = len(self.trace(point="write.syscall"))
        for producer in producers:
            producer.call("emit")
        owner.call("context")
        owner.call("stage-start")
        owner.call("event")
        owner.call("end")
        self.test.assertEqual(self.record(), before)
        self.test.assertEqual(len(self.trace(point="write.syscall")), count)


class ChildProcessFaults(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.build = tempfile.TemporaryDirectory(prefix="sledra-ce04-bin-")
        cls.addClassCleanup(cls.build.cleanup)
        cls.binary = (
            Path(os.environ["SLEDRA_TEST_BIN_DIR"]) / "sledra"
            if os.environ.get("SLEDRA_TEST_BIN_DIR")
            else Path(cls.build.name) / "sledra"
        )
        env = os.environ | {"GOPROXY": "off", "GOSUMDB": "off", "GOTOOLCHAIN": "local"}
        if not os.environ.get("SLEDRA_TEST_BIN_DIR"):
            subprocess.run(
                ["go", "build", "-buildvcs=false", "-o", str(cls.binary), "./cmd/sledra"],
                cwd=ROOT / "recorder",
                env=env,
                check=True,
                capture_output=True,
                timeout=90,
            )
        cls.helper = Path(cls.build.name) / "execution.test"
        subprocess.run(
            ["go", "test", "-c", "-o", str(cls.helper), "./execution"],
            cwd=ROOT / "recorder",
            env=env,
            check=True,
            capture_output=True,
            timeout=90,
        )
        cls.proxy = Path(cls.build.name) / "sledra-test-proxy"
        cls.proxy.write_text(
            '#!/bin/bash\nexec "$CE04_TEST_BINARY" -test.run=\'^TestCE04ProcessHelper$\' -- "$@"\n'
        )
        cls.proxy.chmod(0o700)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ce04-", dir=Path.home() / ".cache")
        self.work = Path(self.temp.name)
        # LIFO: actors/gates close before durable capture, then temporary cleanup.
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.retain)
        self.env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("SLEDRA_", "_SLEDRA_", "CE04_", "BASH_FUNC_"))
            and k not in {"BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS"}
        } | {
            "SLEDRA_MODE": "execution-v2",
            "SLEDRA_BIN": str(self.proxy),
            "OWNER_LIBRARY": str(ROOT / "integrations/bash/sledra-execution.sh"),
            "EVENT_LIBRARY": str(ROOT / "integrations/bash/sledra-events.sh"),
            "ECF_PASS": "synthetic-ce04-secret-not-a-credential",
            "CE04_HELPER": "1",
            "CE04_TEST_BINARY": str(self.helper),
            "CE04_REAL_BIN": str(self.binary),
        }

    def retain(self):
        destination = os.environ.get("SLEDRA_CE04_EVIDENCE")
        if destination:
            shutil.copytree(
                self.work,
                Path(destination) / self.id().split(".")[-1],
                dirs_exist_ok=True,
                symlinks=True,
            )
        # Validate the original coordinator receipt before deliberate transport
        # loss/corruption, against the unchanged CE-01 independent checker.
        for trace in self.work.glob("*/trace.jsonl"):
            for line in trace.read_bytes().splitlines():
                row = json.loads(line)
                if row["point"] == "receipt":
                    contract.receipt(row["wire"].encode(), row["status"], command=row["command"])

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
                "ce04",
                "--partition-timezone=+00:00",
            ],
            check=True,
            capture_output=True,
            timeout=10,
        )

    def test_serial_independent_producers_preserve_every_complete_fact(self):
        c = Case(self, "serial")
        owner = c.owner()
        p0 = c.producer(owner)
        p1 = c.producer(owner, "p1")
        owner.call("context")
        owner.call("context")
        owner.call("stage-start")
        for producer in [p0, p1, p0, p1]:
            producer.call("emit")
        owner.call("stage-end")
        owner.call("end")
        owner.call("end")
        owner.call("conflicting-end")
        events = c.events()
        self.assertEqual(
            [e["name"] for e in events],
            [
                "execution.started",
                "execution.context",
                "stage.started",
                *["synthetic.ready"] * 4,
                "stage.ended",
                "execution.ended",
            ],
        )
        writes = c.trace(point="write.return")
        self.assertEqual(c.record(), b"".join(r["record"].encode() for r in writes))
        self.assertEqual(len(writes), 9)
        self.assertEqual(c.state()["phase"], "closed")
        self.assertFalse(c.trace(command="legacy"))

    def test_busy_has_zero_writes_no_retry_and_other_execution_remains_independent(self):
        c = Case(self, "busy")
        owner = c.owner()
        p0 = c.producer(owner)
        p1 = c.producer(owner, "p1")
        gate = Gate(c)
        p0.send("emit", pause="record.before-write")
        gate.wait()
        p1.call("emit")
        owner.call("context")
        owner.call("stage-start")
        self.assertEqual([r["status"] for r in c.trace(actor="p1", point="receipt")], [21])
        c.assert_one_fact("p1", writes=0)
        other = Case(self, "independent")
        other_owner = other.owner()
        other.producer(other_owner).call("emit")
        other_owner.call("end")
        self.assertEqual(len(other.events()), 3)
        gate.release()
        p0.done("emit")
        p1.call("emit")
        owner.call("end")
        self.assertEqual(
            [e["name"] for e in c.events()],
            ["execution.started", "synthetic.ready", "synthetic.ready", "execution.ended"],
        )
        self.assertFalse(c.trace(actor="p1", command="result"))
        self.assertFalse(c.trace(actor="p1", command="disable"))

    def test_intent_data_and_state_publication_faults_disable_every_new_writer(self):
        points = [
            "state-intent.before-write",
            "state-intent.after-write",
            "state-intent.close",
            "state-intent.before-rename",
            "state-intent.after-rename",
            "record.before-open",
            "record.before-write",
            "record.after-write",
            "record.after-close",
            "state-commit.before-write",
            "state-commit.after-write",
            "state-commit.close",
            "state-commit.before-rename",
            "state-commit.after-rename",
        ]
        for point in points:
            with self.subTest(point=point):
                c = Case(self, point)
                owner = c.owner()
                p0 = c.producer(owner)
                p1 = c.producer(owner, "p1")
                before = c.record()
                p0.call("emit", faults=point)
                record = c.assert_one_fact(
                    "p0",
                    writes=0
                    if point.startswith("state-intent")
                    or point in {"record.before-open", "record.before-write"}
                    else 1,
                )
                self.assertEqual(
                    c.record(),
                    before + (record if c.trace(actor="p0", point="write.syscall") else b""),
                )
                self.assertEqual(len(c.trace(actor="p0", point="fault")), 1)
                self.assertEqual(
                    c.trace(actor="p0", point="receipt", command="commit")[0]["status"], 23
                )
                c.assert_stopped(owner, p0, p1)

    def test_short_write_full_write_error_and_close_never_repair_or_repeat(self):
        for mode in ["short", "write-error", "full-error", "close"]:
            with self.subTest(mode=mode):
                c = Case(self, mode)
                owner = c.owner()
                p0 = c.producer(owner)
                p1 = c.producer(owner, "p1")
                before = c.record()
                p0.call("emit", io=mode)
                record = c.assert_one_fact("p0", writes=0 if mode == "write-error" else 1)
                extra = record[:5] if mode == "short" else b"" if mode == "write-error" else record
                self.assertEqual(c.record(), before + extra)
                self.assertEqual(len(c.trace(actor="p0", point="write.enter")), 1)
                self.assertEqual(len(c.trace(actor="p0", point="close.return")), 1)
                c.assert_stopped(owner, p0, p1)
                if mode != "short":
                    c.events()

    def test_sigkill_at_each_durable_transition_uses_one_result_never_replays(self):
        points = [
            "state-intent.before-write",
            "state-intent.after-write",
            "state-intent.close",
            "state-intent.before-rename",
            "state-intent.after-rename",
            "record.before-open",
            "record.before-write",
            "write.after-return",
            "close.after-return",
            "state-commit.before-write",
            "state-commit.after-write",
            "state-commit.close",
            "state-commit.before-rename",
            "state-commit.after-rename",
        ]
        for point in points:
            with self.subTest(point=point):
                c = Case(self, point)
                owner = c.owner()
                p0 = c.producer(owner)
                p1 = c.producer(owner, "p1")
                gate = Gate(c)
                before = c.record()
                p0.send("emit", pause=point)
                gate.wait()
                gate.kill()
                p0.done("emit")
                written = point in {"write.after-return", "close.after-return"} or point.startswith(
                    "state-commit"
                )
                record = c.assert_one_fact("p0", writes=int(written))
                self.assertEqual(c.record(), before + (record if written else b""))
                self.assertEqual(len(c.trace(actor="p0", command="result", point="call")), 1)
                if point == "state-commit.after-rename":
                    self.assertFalse(c.trace(actor="p0", command="disable", point="call"))
                    self.assertEqual(
                        c.trace(actor="p0", point="receipt", command="result")[0]["result"],
                        "confirmed",
                    )
                    p1.call("emit")
                    owner.call("end")
                else:
                    self.assertEqual(len(c.trace(actor="p0", command="disable", point="call")), 1)
                    c.assert_stopped(owner, p0, p1)

    def test_closing_at_every_admission_boundary_keeps_first_end_and_blocks_new_facts(self):
        points = [
            "admission.before-gates",
            "admission.after-disabled",
            "admission.accepted",
            "state-intent.after-rename",
            "record.before-write",
            "write.after-return",
            "close.after-return",
            "state-commit.after-rename",
        ]
        for point in points:
            with self.subTest(point=point):
                c = Case(self, point)
                owner = c.owner()
                p0 = c.producer(owner)
                p1 = c.producer(owner, "p1")
                gate = Gate(c)
                before = c.record()
                p0.send("emit", pause=point)
                gate.wait()
                self.assertEqual(owner.call("end"), "closing")
                claim = (c.session() / "closing").read_bytes()
                self.assertEqual(
                    c.trace(actor="owner", point="receipt", kind="end")[0]["status"], 21
                )
                p1.call("emit")
                self.assertFalse(c.trace(actor="p1", point="write.syscall"))
                gate.release()
                p0.done("emit")
                admitted = point not in {"admission.before-gates", "admission.after-disabled"}
                record = c.assert_one_fact("p0", writes=int(admitted))
                self.assertEqual(c.record(), before + (record if admitted else b""))
                owner.call("end")
                owner.call("conflicting-end")
                owner.call("end")
                self.assertEqual((c.session() / "closing").read_bytes(), claim)
                self.assertEqual(len(c.trace(actor="owner", point="prepared", kind="end")), 1)
                self.assertEqual(len(c.trace(actor="owner", point="write.syscall", kind="end")), 1)
                end = c.trace(actor="owner", point="prepared", kind="end")[0]["record"].encode()
                self.assertEqual(c.record(), before + (record if admitted else b"") + end)
                self.assertEqual(c.events()[-1]["name"], "execution.ended")
                c.assert_stopped(owner, p0, p1)

    def test_disabled_request_and_quiescence_distinguish_the_one_admitted_writer(self):
        points = [
            "admission.before-gates",
            "admission.after-disabled",
            "admission.accepted",
            "state-intent.after-rename",
            "record.before-open",
            "record.before-write",
            "write.after-return",
            "close.after-return",
            "state-commit.after-rename",
        ]
        for point in points:
            with self.subTest(point=point):
                c = Case(self, point)
                owner = c.owner()
                p0 = c.producer(owner)
                p1 = c.producer(owner, "p1")
                gate = Gate(c)
                before = c.record()
                p0.send("emit", pause=point)
                gate.wait()
                stopped = c.direct(owner, "disable")
                self.assertEqual(stopped.returncode, 23)
                self.assertEqual(stopped.stdout.split(b"\t")[7], b"requested")
                p1.call("emit")
                self.assertFalse(c.trace(actor="p1", point="write.syscall"))
                gate.release()
                p0.done("emit")
                written = point in {
                    "record.before-open",
                    "record.before-write",
                    "write.after-return",
                    "close.after-return",
                    "state-commit.after-rename",
                }
                record = c.assert_one_fact("p0", writes=int(written))
                self.assertEqual(c.record(), before + (record if written else b""))
                final = c.direct(owner, "disable")
                self.assertEqual(final.stdout.split(b"\t")[7], b"quiescent")
                c.assert_stopped(owner, p0, p1)
                rows = c.trace()
                quiescent = max(
                    i
                    for i, r in enumerate(rows)
                    if r["point"] == "receipt" and r.get("stop") == "quiescent"
                )
                self.assertFalse(any(r["point"] == "write.syscall" for r in rows[quiescent + 1 :]))
                if point in {"record.before-open", "record.before-write"}:
                    requested = next(
                        i
                        for i, r in enumerate(rows)
                        if r["point"] == "receipt" and r.get("stop") == "requested"
                    )
                    invocation = next(
                        i
                        for i, r in enumerate(rows)
                        if r["actor"] == "p0" and r["point"] == "write.syscall"
                    )
                    self.assertLess(requested, invocation)
                if point == "state-commit.after-rename":
                    fact = c.trace(actor="p0", point="prepared", kind="event")[0]
                    lookup = c.direct(
                        owner,
                        "result",
                        prepared=self.prepared(fact),
                        request_id=fact["request_id"],
                        event_id=fact["event_id"],
                        record_sha256=fact["digest"],
                    )
                    self.assertEqual(lookup.returncode, 0)
                    self.assertEqual(lookup.stdout.split(b"\t")[1], b"confirmed")
                    self.assertEqual(lookup.stdout.split(b"\t")[6:8], [b"disabled", b"quiescent"])
                    c.assert_stopped(owner, p0, p1)

    @staticmethod
    def prepared(row):
        record = row["record"].encode().removesuffix(b"\n")
        assert hashlib.sha256(record).hexdigest() == row["digest"]
        return (
            "\t".join(
                [
                    "1",
                    row["request_id"],
                    row["execution_id"],
                    row["event_id"],
                    "-",
                    "-",
                    "-",
                    row["digest"],
                    record.decode(),
                ]
            ).encode()
            + b"\n"
        )

    def test_receipt_loss_confirmed_overwritten_busy_and_unreadable_use_one_lookup(self):
        for scenario in [
            "confirmed",
            "overwritten",
            "busy",
            "state-missing",
            "result-lost",
            "malformed",
        ]:
            with self.subTest(scenario=scenario):
                c = Case(self, scenario)
                owner = c.owner()
                p0 = c.producer(
                    owner, env={"CE04_BAD_RECEIPT": "1"} if scenario == "malformed" else {}
                )
                gate = Gate(c)
                second = Gate(c, "second.sock")
                p1 = c.producer(owner, "p1", env={"CE04_SOCKET": str(second.path)})
                before = c.record()
                # Only the first command in the target list pauses; both commit
                # and result can lose output without a recursive lookup.
                p0.send(
                    "emit",
                    pause="receipt.before-delivery",
                    drop="0" if scenario == "malformed" else "1",
                    target="commit,result" if scenario == "result-lost" else "commit",
                )
                gate.wait()
                if scenario == "overwritten":
                    p1.call("emit")
                if scenario == "state-missing":
                    (c.session() / "state.json").unlink()
                if scenario == "busy":
                    p1.send("emit", pause="record.before-write")
                    second.wait()
                gate.release()
                p0.done("emit")
                record = c.assert_one_fact("p0")
                self.assertEqual(len(c.trace(actor="p0", point="call", command="result")), 1)
                unknown = scenario not in {"confirmed", "malformed"}
                self.assertEqual(
                    len(c.trace(actor="p0", point="call", command="disable")), int(unknown)
                )
                if scenario == "busy":
                    self.assertEqual(
                        c.trace(actor="p0", point="receipt", command="result")[0]["status"], 24
                    )
                    self.assertEqual(
                        c.trace(actor="p0", point="receipt", command="disable")[0]["stop"],
                        "requested",
                    )
                    second.release()
                    p1.done("emit")
                if unknown:
                    c.assert_stopped(owner, p0, p1)
                else:
                    self.assertEqual(c.record(), before + record)
                    p1.call("emit")
                    owner.call("end")

    def test_end_writer_kill_never_duplicates_ended_or_completes_an_uncertain_close(self):
        for point in [
            "state-intent.after-rename",
            "write.after-return",
            "close.after-return",
            "state-commit.after-rename",
        ]:
            with self.subTest(point=point):
                c = Case(self, point)
                owner = c.owner()
                p0 = c.producer(owner)
                gate = Gate(c)
                owner.call("stage-start")
                before = c.record()
                owner.send("end", pause=point, kind="end")
                gate.wait()
                gate.kill()
                owner.done("end")
                owner.call("end")
                owner.call("conflicting-end")
                p0.call("emit")
                end = c.trace(actor="owner", point="prepared", kind="end")
                self.assertEqual(len(end), 1)
                self.assertEqual(
                    len(c.trace(actor="owner", point="write.syscall", kind="end")),
                    int(point != "state-intent.after-rename"),
                )
                self.assertEqual(
                    c.record(),
                    before
                    + (end[0]["record"].encode() if point != "state-intent.after-rename" else b""),
                )
                self.assertNotIn("stage.ended", [e["name"] for e in c.events()])
                self.assertEqual(len(c.trace(actor="owner", point="call", command="result")), 1)
                self.assertEqual(
                    len(c.trace(actor="owner", point="call", command="disable")),
                    int(point != "state-commit.after-rename"),
                )

    def test_start_failures_never_publish_a_reference_or_retry_open(self):
        scenarios = (
            [
                ("fault", p)
                for p in [
                    "state-create.before-write",
                    "state-create.after-rename",
                    "state-intent.before-write",
                    "state-intent.after-rename",
                    "state-commit.before-rename",
                    "state-commit.after-rename",
                    "descriptor-publish.before-write",
                    "descriptor-publish.close",
                    "descriptor-publish.before-rename",
                    "descriptor-publish.after-rename",
                ]
            ]
            + [("io", p) for p in ["short", "write-error", "full-error", "close"]]
            + [
                ("kill", p)
                for p in [
                    "state-intent.after-rename",
                    "write.after-return",
                    "close.after-return",
                    "descriptor-publish.before-rename",
                ]
            ]
            + [("drop", "receipt.before-delivery")]
        )
        for operation, point in scenarios:
            with self.subTest(operation=operation, point=point):
                c = Case(self, operation + "-" + point)
                env = {"CE04_COMMAND": "open"}
                if operation == "fault":
                    env["CE04_FAULTS"] = point
                if operation == "io":
                    env["CE04_IO_FAULT"] = point
                if operation == "drop":
                    env["CE04_DROP_RECEIPT"] = "1"
                if operation == "kill":
                    gate = Gate(c)
                    env["CE04_PAUSE"] = point
                    owner = Actor(c, "owner", env=env)
                    gate.wait()
                    gate.kill()
                    owner.ready()
                else:
                    owner = c.owner(env=env)
                self.assertEqual(owner.reference, "-")
                self.assertEqual(owner.state, "disabled")
                before = c.record()
                owner.call("event")
                owner.call("end")
                self.assertEqual(c.record(), before)
                self.assertEqual(len(c.trace(point="call", command="open")), 1)
                self.assertFalse(c.trace(point="call", command="commit"))
                self.assertFalse(c.trace(point="call", command="result"))
                self.assertLessEqual(len(c.trace(point="write.syscall")), 1)
                if operation == "io" and point == "short":
                    self.assertEqual(len(before), 5)

    def test_many_producers_contend_without_interleaved_or_duplicated_records(self):
        c = Case(self, "many")
        owner = c.owner()
        producers = [c.producer(owner, f"p{i}") for i in range(6)]
        gate = Gate(c)
        producers[0].send("emit", pause="record.before-write")
        gate.wait()
        for p in producers[1:]:
            p.send("emit")
        for p in producers[1:]:
            p.done("emit")
        gate.release()
        producers[0].done("emit")
        for _ in range(5):
            for p in producers:
                p.send("emit")
            owner.send("context")
            for p in producers:
                p.done("emit")
            owner.done("context")
        owner.call("context")
        owner.call("end")
        rows = c.trace()
        receipts = c.trace(point="receipt", command="commit")
        self.assertTrue(all(r["status"] in {0, 21} for r in receipts))
        self.assertEqual(
            c.record(), b"".join(r["record"].encode() for r in c.trace(point="write.return"))
        )
        facts = c.events()
        self.assertEqual(len({e["event_id"] for e in facts}), len(facts))
        self.assertEqual(len(c.trace(point="prepared", kind="event")), 36)
        self.assertEqual(len(c.trace(point="call", command="commit", kind="event")), 36)
        self.assertFalse(any(r["command"] in {"result", "disable", "legacy"} for r in rows))
        self.assertEqual(c.state()["phase"], "closed")

    def test_control_damage_identity_mismatch_and_record_replacement_never_rebuild(self):
        scenarios = [
            "state-missing",
            "state-corrupt",
            "state-oversize",
            "state-mode",
            "state-symlink",
            "state-hardlink",
            "descriptor-missing",
            "descriptor-corrupt",
            "lock-missing",
            "lock-replaced",
            "record-missing",
            "record-partial",
            "record-replaced",
            "boot",
            "namespace",
            "start-ticks",
            "uid",
            "owner-pid",
        ]
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                c = Case(self, scenario)
                owner = c.owner()
                p0 = c.producer(owner)
                p1 = c.producer(owner, "p1")
                session = c.session()
                state = session / "state.json"
                descriptor = session / "event-context.json"
                lock = session / "writer.lock"
                record = next(c.records.glob("executions/*/*/*.jsonl"))
                if scenario == "state-missing":
                    state.unlink()
                elif scenario == "state-corrupt":
                    state.write_bytes(b"{broken")
                elif scenario == "state-oversize":
                    state.write_bytes(b"x" * 524289)
                elif scenario == "state-mode":
                    state.chmod(0o644)
                elif scenario == "state-symlink":
                    state.rename(session / "saved-state")
                    state.symlink_to("saved-state")
                elif scenario == "state-hardlink":
                    os.link(state, session / "saved-state")
                elif scenario == "descriptor-missing":
                    descriptor.unlink()
                elif scenario == "descriptor-corrupt":
                    descriptor.write_bytes(b"{}")
                elif scenario == "lock-missing":
                    lock.unlink()
                elif scenario == "lock-replaced":
                    lock.rename(session / "saved-lock")
                    lock.touch(mode=0o600)
                elif scenario == "record-missing":
                    record.unlink()
                elif scenario == "record-partial":
                    with record.open("ab") as stream:
                        stream.write(b"partial")
                elif scenario == "record-replaced":
                    record.rename(session / "saved-record")
                    record.write_bytes(b"replacement\n")
                    record.chmod(0o600)
                else:
                    desc = json.loads(descriptor.read_bytes())
                    if scenario == "boot":
                        desc["boot_id"] = "40000000-0000-4000-8000-000000000001"
                    elif scenario == "namespace":
                        desc["pid_namespace_inode"] = str(int(desc["pid_namespace_inode"]) + 1)
                    elif scenario == "start-ticks":
                        desc["owner_start_ticks"] = str(int(desc["owner_start_ticks"]) + 1)
                    elif scenario == "uid":
                        desc["uid"] += 1
                    else:
                        desc["owner_pid"] = 2147483647
                    descriptor.write_text(json.dumps(desc))
                metadata = {}
                for path in [state, descriptor, lock, record]:
                    if path.exists() or path.is_symlink():
                        info = path.lstat()
                        metadata[path.name] = {
                            "inode": info.st_ino,
                            "links": info.st_nlink,
                            "mode": info.st_mode,
                            "symlink": os.readlink(path) if path.is_symlink() else None,
                        }
                (c.path / "fault-metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
                before = c.record()
                p0.call("emit")
                c.assert_stopped(owner, p0, p1)
                self.assertEqual(c.record(), before)
                self.assertFalse(c.trace(actor="p0", point="write.syscall"))
                if scenario.endswith("missing"):
                    target = {
                        "state-missing": state,
                        "descriptor-missing": descriptor,
                        "lock-missing": lock,
                        "record-missing": record,
                    }[scenario]
                    self.assertFalse(target.exists())
                if scenario == "state-mode":
                    self.assertEqual(state.stat().st_mode & 0o777, 0o644)
                if scenario == "state-symlink":
                    self.assertTrue(state.is_symlink())

    def test_disable_publication_fallback_and_double_failure_report_only_actual_scope(self):
        for both in [False, True]:
            with self.subTest(both=both):
                c = Case(self, "both" if both else "fallback")
                owner = c.owner()
                p0 = c.producer(owner)
                p1 = c.producer(owner, "p1")
                before = c.record()
                faults = "disabled.publish" + (",state-disable.before-write" if both else "")
                stop = c.direct(
                    owner, "disable", env={"CE04_COMMAND": "disable", "CE04_FAULTS": faults}
                )
                self.assertEqual(stop.returncode, 23)
                self.assertEqual(stop.stdout.split(b"\t")[7], b"local" if both else b"quiescent")
                self.assertFalse((c.session() / "disabled").exists())
                self.assertEqual(c.state()["phase"], "active" if both else "disabled")
                if both:
                    # This counterexample is the declared storage boundary, not a
                    # successful shared stop: an independent healthy writer can append.
                    self.assertEqual(stop.stdout.split(b"\t")[2], b"disable_publish_failed")
                    p1.call("emit")
                    self.assertGreater(len(c.record()), len(before))
                    owner.call("end")
                else:
                    c.assert_stopped(owner, p0, p1)

    def test_dead_owner_stale_reference_and_same_pid_exec_have_explicit_boundaries(self):
        c = Case(self, "dead")
        owner = c.owner()
        p0 = c.producer(owner)
        p1 = c.producer(owner, "p1")
        before = c.record()
        owner.process.send_signal(signal.SIGTERM)
        self.assertEqual(owner.process.wait(timeout=5), -signal.SIGTERM)
        p0.call("emit")
        p1.call("emit")
        self.assertEqual(c.record(), before)
        self.assertTrue(
            all(r["code"] == "owner_gone" for r in c.trace(point="receipt", command="commit"))
        )
        self.assertEqual(len(list(c.runtime.iterdir())), 1)
        fresh = Case(self, "fresh")
        new_owner = fresh.owner()
        self.assertNotEqual(new_owner.execution, owner.execution)
        new_owner.call("end")
        e = Case(self, "exec")
        script = r"""
set -aeuo pipefail
source "$OWNER_LIBRARY"
sledra_execution_begin --child-events
printf '%s|%s\n' "$BASHPID" "$_SLEDRA_V2_ID"
exec bash -c 'source "$OWNER_LIBRARY"; sledra_execution_begin --child-events; printf "%s|%s\n" "$BASHPID" "$_SLEDRA_V2_ID"; sledra_execution_end --exit-code 0'
"""
        result = subprocess.run(
            ["bash", "-c", script],
            env=e.env | {"CE04_ACTOR": "owner-exec"},
            capture_output=True,
            timeout=15,
        )
        self.assertEqual((result.returncode, result.stderr), (0, b""))
        (e.path / "exec.stdout").write_bytes(result.stdout)
        first, second = [row.split(b"|") for row in result.stdout.splitlines()]
        self.assertEqual(first[0], second[0])
        self.assertNotEqual(first[1], second[1])
        self.assertEqual(len(list(e.runtime.glob("*/event-context.json"))), 2)
        # PID/starttime cannot detect an arbitrary same-PID exec. This asserts new
        # begin identity and no inherited owner secret, not old-reference revocation.

    def test_original_reader_reports_a_partial_tail_without_repair(self):
        c = Case(self, "reader")
        owner = c.owner()
        p0 = c.producer(owner)
        p0.call("emit", io="short")
        before = c.record()
        with FileReader(c.records) as reader:
            items = list(reader.iter_events(None, None))
        self.assertEqual(
            [item.code for item in items if isinstance(item, ReadIssue)], ["incomplete_tail"]
        )
        self.assertEqual(len([item for item in items if isinstance(item, Fact)]), 1)
        self.assertEqual(c.record(), before)
        owner.call("end")
        self.assertEqual(c.record(), before)

    def test_production_binary_ignores_all_private_test_controls(self):
        c = Case(self, "production")
        owner = c.owner(
            env={
                "SLEDRA_BIN": str(self.binary),
                "CE04_PAUSE": "record.before-write",
                "CE04_FAULTS": "state-intent.before-write",
                "CE04_IO_FAULT": "short",
                "CE04_DROP_RECEIPT": "1",
            }
        )
        p0 = c.producer(
            owner,
            env={
                "SLEDRA_BIN": str(self.binary),
                "CE04_IO_FAULT": "short",
                "CE04_DROP_RECEIPT": "1",
            },
        )
        p0.call("emit")
        owner.call("context")
        owner.call("end")
        self.assertEqual(
            [e["name"] for e in c.events()],
            ["execution.started", "synthetic.ready", "execution.context", "execution.ended"],
        )
        self.assertFalse(c.trace())

    def parent_child(self, case, *, point="", io=""):
        fifo = case.path / "child-go"
        os.mkfifo(fifo, 0o600)
        self.addCleanup(lambda: fifo.unlink(missing_ok=True))
        script = r"""
set -Eeuo pipefail
source "$OWNER_LIBRARY"
source "$EVENT_LIBRARY"
sledra_execution_begin --child-events
printf 'READY|%s|%s|%s\n' "$SLEDRA_EVENT_CONTEXT" "$_SLEDRA_V2_ID" "$_SLEDRA_V2_STATE"
(
    export CE04_ACTOR=child CE04_PAUSE="$CHILD_PAUSE" CE04_IO_FAULT="$CHILD_IO"
    sledra_event_attach
    printf 'ATTACHED\n'
    IFS= read -r go < "$CHILD_GO"
    sledra_event_emit synthetic.child --int lead 3
    printf 'CHILD-DONE\n'
) &
child_pid=$!
printf 'CHILD-PID|%s\n' "$child_pid"
child_rc=0
while IFS='|' read -r action rest; do
    case $action in
        end) sledra_execution_end --exit-code "$child_rc" ;;
        context) sledra_execution_context --task synthetic.ce04 --cycle-kind none ;;
        wait-child)
            if wait "$child_pid"; then child_rc=0; else child_rc=$?; fi
            printf 'DONE|wait-child|%s\n' "$child_rc"
            continue ;;
        quit) break ;;
        *) exit 90 ;;
    esac
    printf 'DONE|%s|%s\n' "$action" "$_SLEDRA_V2_STATE"
done
"""
        actor = Actor(
            case,
            "owner-parent",
            script=script,
            env={"CHILD_PAUSE": point, "CHILD_IO": io, "CHILD_GO": str(fifo)},
        ).ready()
        messages = [actor.read(), actor.read()]
        self.assertIn(["ATTACHED"], messages)
        pid = int(next(m[1] for m in messages if m[0] == "CHILD-PID"))
        stat = (Path("/proc") / str(pid) / "stat").read_text()
        self.assertEqual(int(stat[stat.rfind(")") + 2 :].split()[1]), actor.process.pid)
        fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
        os.write(fd, b"go\n")
        os.close(fd)
        return actor, pid

    def test_actual_parent_child_fault_and_cli_kill_stop_the_parent_without_replay(self):
        for fault in ["short", "kill"]:
            with self.subTest(fault=fault):
                c = Case(self, fault)
                gate = Gate(c)
                parent, _ = self.parent_child(
                    c,
                    point="state-intent.after-rename" if fault == "kill" else "",
                    io="short" if fault == "short" else "",
                )
                if fault == "kill":
                    gate.wait()
                    gate.kill()
                self.assertEqual(parent.read(), ["CHILD-DONE"])
                self.assertEqual(parent.call("wait-child"), "0")
                before = c.record()
                parent.call("context")
                parent.call("end")
                self.assertEqual(c.record(), before)
                self.assertEqual(len(c.trace(actor="child", point="call", command="commit")), 1)
                self.assertEqual(
                    len(c.trace(actor="child", point="call", command="result")),
                    int(fault == "kill"),
                )
                self.assertEqual(
                    len(c.trace(actor="child", point="call", command="disable")),
                    int(fault == "kill"),
                )
                self.assertFalse(
                    c.trace(actor="owner-parent", point="write.syscall", command="commit")
                )

    def test_actual_parent_end_busy_drains_the_child_before_ended(self):
        c = Case(self, "parent-end")
        gate = Gate(c)
        parent, _ = self.parent_child(c, point="record.before-write")
        gate.wait()
        self.assertEqual(parent.call("end"), "closing")
        gate.release()
        self.assertEqual(parent.read(), ["CHILD-DONE"])
        self.assertEqual(parent.call("wait-child"), "0")
        self.assertEqual(parent.call("end"), "closed")
        self.assertEqual(
            [e["name"] for e in c.events()],
            ["execution.started", "synthetic.child", "execution.ended"],
        )
        self.assertEqual(len(c.trace(actor="owner-parent", point="prepared", kind="end")), 1)
        self.assertEqual(len(c.trace(actor="child", point="write.syscall")), 1)

    def test_signaled_child_shell_preserves_exit_status_while_admitted_go_writer_drains(self):
        for number in [signal.SIGTERM, signal.SIGKILL]:
            with self.subTest(signal=number):
                c = Case(self, number.name)
                gate = Gate(c)
                parent, pid = self.parent_child(
                    c, point="record.before-write,receipt.before-delivery"
                )
                gate.wait()
                os.kill(pid, number)
                expected = 128 + number
                self.assertEqual(parent.call("wait-child"), str(expected))
                self.assertEqual(parent.call("end"), "closing")
                gate.release()
                self.assertEqual(gate.wait()["point"], "receipt.before-delivery")
                self.assertEqual(parent.call("end"), "closed")
                self.assertEqual(c.events()[-1]["attributes"], {"exit_code": expected})
                self.assertEqual(len(c.trace(actor="child", point="write.syscall")), 1)
                self.assertFalse(c.trace(actor="child", point="call", command="result"))
                gate.release()

    def test_closing_publication_failure_and_invalid_end_never_invent_ended(self):
        for point in [
            "closing-publish.before-write",
            "closing-publish.after-write",
            "closing-publish.close",
            "closing-publish.before-rename",
            "closing-publish.after-rename",
        ]:
            with self.subTest(point=point):
                c = Case(self, point)
                owner = c.owner()
                p0 = c.producer(owner)
                owner.call("invalid-end")
                self.assertFalse((c.session() / "closing").exists())
                self.assertFalse(c.trace(actor="owner", point="prepared", kind="end"))
                owner.call("stage-start")
                before = c.record()
                owner.call("end", faults=point, kind="end")
                self.assertEqual(c.record(), before)
                self.assertEqual(len(c.trace(actor="owner", point="prepared", kind="end")), 1)
                self.assertFalse(c.trace(actor="owner", point="write.syscall", kind="end"))
                c.assert_stopped(owner, p0)
                self.assertNotIn("execution.ended", [e["name"] for e in c.events()])

    def test_writing_intent_is_durable_stop_when_disabled_gate_publication_fails(self):
        c = Case(self, "writing-fallback")
        owner = c.owner()
        p0 = c.producer(owner)
        p1 = c.producer(owner, "p1")
        before = c.record()
        p0.call("emit", io="full-error", faults="disabled.publish")
        record = c.assert_one_fact("p0")
        self.assertEqual(c.record(), before + record)
        self.assertFalse((c.session() / "disabled").exists())
        self.assertEqual(c.state()["phase"], "writing")
        receipt = c.trace(actor="p0", point="receipt", command="commit")[0]
        self.assertEqual((receipt["status"], receipt["stop"]), (23, "quiescent"))
        c.assert_stopped(owner, p0, p1)
        self.assertTrue((c.session() / "disabled").exists())

    def test_diagnostic_competition_and_closed_stderr_do_not_change_hook_results(self):
        c = Case(self, "diagnostics")
        owner = c.owner()
        producers = [c.producer(owner, f"p{i}") for i in range(6)]
        for p in producers:
            p.send("invalid")
        for p in producers:
            p.done("invalid")
        for p in producers:
            p.call("invalid")
        errors = b"".join((c.path / f"{p.name}.stderr").read_bytes() for p in producers)
        self.assertEqual(errors, DIAG)
        self.assertNotIn(self.env["ECF_PASS"].encode(), errors)
        self.assertEqual((c.session() / "diagnostic").read_bytes(), b"")
        owner.call("end")
        self.assertEqual(len(c.events()), 2)
        other = Case(self, "stderr-closed")
        parent = other.owner()
        p0 = other.producer(parent)
        p1 = other.producer(parent, "p1")
        p0.call("stderr-close")
        p0.call("invalid")
        p1.call("invalid")
        self.assertEqual(
            (other.path / "p0.stderr").read_bytes() + (other.path / "p1.stderr").read_bytes(), b""
        )
        p1.call("emit")
        parent.call("end")
        self.assertEqual(len(other.events()), 3)
