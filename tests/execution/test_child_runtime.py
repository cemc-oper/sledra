"""CE-02 real Go CLI components, not the future CE-03 public Bash library."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
SPEC = ROOT / "spec/execution-child-events"
_loader = importlib.util.spec_from_file_location("child_reference", SPEC / "ce_contract.py")
reference = importlib.util.module_from_spec(_loader)
_loader.loader.exec_module(reference)


class ChildRuntimeCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.install = tempfile.TemporaryDirectory(prefix="sledra-child-cli-")
        override = os.environ.get("SLEDRA_TEST_BIN_DIR")
        cls.binary = Path(override) / "sledra" if override else Path(cls.install.name) / "sledra"
        if not override:
            subprocess.run(
                ["go", "build", "-buildvcs=false", "-o", str(cls.binary), "./cmd/sledra"],
                cwd=ROOT / "recorder",
                check=True,
                timeout=90,
                env=os.environ | {"GOPROXY": "off", "GOSUMDB": "off", "GOTOOLCHAIN": "local"},
            )
        assert cls.binary.is_file()

    @classmethod
    def tearDownClass(cls):
        cls.install.cleanup()

    def test_capability_exact_handshake_and_bounded_errors(self):
        for argv, expected in [
            (["--interface", "CE-1", "--protocol", "1"], 0),
            (["--interface", "CE-1", "--protocol", "2"], 25),
        ]:
            result = subprocess.run(
                [str(self.binary), "execution", "child-events", "capabilities", *argv],
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, expected)
            if expected == 0:
                reference.capabilities(result.stdout)
                self.assertEqual(result.stderr, b"")
            else:
                reference.receipt(result.stdout, expected)
                self.assertLessEqual(len(result.stderr), 256)
                self.assertNotIn(b"--protocol", result.stderr)

    def run_owner(self, body):
        # /tmp is deliberately excluded by the private ancestor permissions policy.
        with tempfile.TemporaryDirectory(
            prefix="sledra-child-native-", dir=Path.home() / ".cache"
        ) as work:
            base = Path(work)
            runtime = base / "runtime"
            runtime.mkdir(mode=0o700)
            env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("SLEDRA_", "BASH_FUNC_")) and k not in ("BASH_ENV", "ENV")
            }
            env.update(BIN=str(self.binary), RECORDS=str(base / "records"), RUNTIME=str(runtime))
            script = (
                r"""set -euo pipefail
"$BIN" execution init-root --root "$RECORDS" --system synthetic --deployment child-cli --partition-timezone=+00:00
start=$("$BIN" execution child-events prepare start --task synthetic.initial)
printf '%s\n' "$start" > "$RUNTIME/start.prepared"
IFS=$'\t' read -r version request execution event entry session key digest record <<< "$start"
opened=$("$BIN" execution child-events open --root "$RECORDS" --runtime-dir "$RUNTIME" --owner-pid "$$" <<< "$start")
IFS=$'\t' read -r version result code request event revision phase stop ref <<< "$opened"
printf '%s\n' "$opened" > "$RUNTIME/open.receipt"
"$BIN" execution child-events attach --context "$ref" > "$RUNTIME/attach.receipt"
"""
                + body
            )
            result = subprocess.run(
                ["bash", "-c", script], env=env, capture_output=True, timeout=30
            )
            self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))
            facts = {}
            for path in runtime.rglob("*"):
                if path.is_file() and path.name not in ("start.prepared", "writer.lock"):
                    facts[str(path.relative_to(runtime))] = path.read_bytes()
            # The owner Bash has exited; a fresh CLI must reject its old reference.
            ref = files_ref = facts["open.receipt"].decode().rstrip("\n").split("\t")[-1]
            gone = subprocess.run(
                [str(self.binary), "execution", "child-events", "attach", "--context", ref],
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(gone.returncode, 26, (files_ref, gone.stdout, gone.stderr))
            reference.receipt(gone.stdout, 26)
            rows = [
                json.loads(line)
                for path in (base / "records").glob("executions/*/*/*.jsonl")
                for line in path.read_bytes().splitlines()
            ]
            return rows, facts

    def test_native_owner_sequential_child_calls_close_and_old_reader(self):
        rows, files = self.run_owner(r"""
context=$("$BIN" execution child-events prepare context --execution-id "$execution" --task synthetic.initial --cycle-kind none --int exact 9007199254740993)
"$BIN" execution child-events commit --context "$ref" --role owner --owner-key "$key" <<< "$context" > "$RUNTIME/context.receipt"
for lead in 0 3 6 9 12; do
    prepared=$("$BIN" execution child-events prepare event gfs.modelvar.ready --execution-id "$execution" --int forecast_lead_hours "$lead")
    bash -c '"$BIN" execution child-events commit --context "$1" --role event <<< "$2"' child "$ref" "$prepared" > "$RUNTIME/event-$lead.receipt"
done
IFS=$'\t' read -r version request identity event aux sid secret digest record <<< "$prepared"
"$BIN" execution child-events result --context "$ref" --request-id "$request" --event-id "$event" --record-sha256 "$digest" <<< "$prepared" > "$RUNTIME/result.receipt"
ended=$("$BIN" execution child-events prepare end --execution-id "$execution" --exit-code 0)
"$BIN" execution child-events commit --context "$ref" --role owner --owner-key "$key" <<< "$ended" > "$RUNTIME/end.receipt"
"$BIN" execution child-events commit --context "$ref" --role owner --owner-key "$key" <<< "$ended" > "$RUNTIME/same-end.receipt"
if "$BIN" execution child-events commit --context "$ref" --role event <<< "$prepared" > "$RUNTIME/closed.receipt"; then exit 90; else test "$?" = 22; fi
""")
        self.assertEqual(
            [
                r["attributes"]["forecast_lead_hours"]
                for r in rows
                if r["name"] == "gfs.modelvar.ready"
            ],
            [0, 3, 6, 9, 12],
        )
        self.assertEqual(rows[1]["attributes"]["parameters"]["exact"], 9007199254740993)
        self.assertEqual(rows[-1]["name"], "execution.ended")
        self.assertEqual(len(rows), 8)
        for name, raw in files.items():
            if name.endswith(".receipt"):
                command = (
                    "open"
                    if name == "open.receipt"
                    else "attach"
                    if name == "attach.receipt"
                    else "result"
                    if name == "result.receipt"
                    else "commit"
                )
                reference.receipt(raw, 22 if name == "closed.receipt" else 0, command)
            elif name.endswith("event-context.json"):
                reference.validate("descriptor", raw)
            elif name.endswith("state.json"):
                reference.validate("state", raw)
        stream = b"".join(json.dumps(row, separators=(",", ":")).encode() + b"\n" for row in rows)
        self.assertEqual(reference.legacy.stream_conflicts(stream), [])

    def test_role_rejection_receipt_lookup_overwrite_and_shared_disable(self):
        rows, files = self.run_owner(r"""
end=$("$BIN" execution child-events prepare end --execution-id "$execution" --exit-code 0)
if "$BIN" execution child-events commit --context "$ref" --role event <<< "$end" > "$RUNTIME/denied.receipt"; then exit 90; else test "$?" = 20; fi
first=$("$BIN" execution child-events prepare event synthetic.ready --execution-id "$execution" --int value 1)
"$BIN" execution child-events commit --context "$ref" --role event <<< "$first" > "$RUNTIME/first.receipt"
second=$("$BIN" execution child-events prepare event synthetic.ready --execution-id "$execution" --int value 2)
"$BIN" execution child-events commit --context "$ref" --role event <<< "$second" > "$RUNTIME/second.receipt"
IFS=$'\t' read -r version request identity event aux sid secret digest record <<< "$first"
if "$BIN" execution child-events result --context "$ref" --request-id "$request" --event-id "$event" --record-sha256 "$digest" <<< "$first" > "$RUNTIME/unknown.receipt"; then exit 90; else test "$?" = 24; fi
if "$BIN" execution child-events disable --context "$ref" > "$RUNTIME/disable.receipt"; then exit 90; else test "$?" = 23; fi
if "$BIN" execution child-events commit --context "$ref" --role event <<< "$second" > "$RUNTIME/stopped.receipt"; then exit 90; else test "$?" = 23; fi
""")
        self.assertEqual(len(rows), 3)
        for name, code in [
            ("denied.receipt", 20),
            ("unknown.receipt", 24),
            ("disable.receipt", 23),
            ("stopped.receipt", 23),
        ]:
            reference.receipt(files[name], code)

    def test_prepare_rejects_duplicate_and_previous_event_without_files(self):
        for args, expected in [
            (
                [
                    "event",
                    "synthetic.ready",
                    "--execution-id",
                    "10000000-0000-4000-8000-000000000001",
                    "--int",
                    "lead",
                    "3",
                    "--string",
                    "lead",
                    "3",
                ],
                "duplicate_key",
            ),
            (["context", "--previous-event", "DO_NOT_ECHO"], "usage"),
            (["event", "execution.ended"], "reserved_event_name"),
        ]:
            result = subprocess.run(
                [str(self.binary), "execution", "child-events", "prepare", *args],
                capture_output=True,
                timeout=10,
            )
            self.assertEqual(result.returncode, 20)
            parts = reference.receipt(result.stdout, 20)
            self.assertEqual(parts[2], expected)
            self.assertNotIn(b"DO_NOT_ECHO", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
