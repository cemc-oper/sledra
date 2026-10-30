"""Independent CE-1 vectors; these assertions do not exercise a CE runtime."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from ce_contract import (
    ContractError,
    LIMITS,
    ROOT,
    SCHEMA,
    SPEC,
    absolute,
    authorize,
    argv_payload,
    binding,
    bounded,
    capabilities,
    latch,
    legacy,
    prepared,
    receipt,
    reference,
    validate,
)
from semantics import admission, compatibility, diagnostic, recovery, typed_values

CASES = SPEC / "cases"
sys.path.insert(0, str(ROOT / "analysis/src"))
from sledra_analysis.execution.reader import Fact, FileReader  # noqa: E402


def load(name):
    return json.loads((CASES / name).read_bytes())


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def materialize(case):
    raw = (CASES / case["base"]).read_bytes()
    if "set" in case or "remove" in case:
        value = json.loads(raw)
        for path, replacement in case.get("set", {}).items():
            parent = value
            keys = path.split("/")
            for key in keys[:-1]:
                parent = parent[key]
            parent[keys[-1]] = replacement
        for key in case.get("remove", []):
            del value[key]
        raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
    if "fields" in case:
        parts = raw[:-1].decode().split("\t")
        for index, value in case["fields"].items():
            parts[int(index)] = value
        raw = ("\t".join(parts) + "\n").encode()
    if "prefix" in case:
        raw = case["prefix"].encode() + raw[1:]
    raw += case.get("suffix", "").encode() + bytes.fromhex(case.get("suffix_hex", ""))
    if "pad_to" in case:
        assert case["pad_to"] >= len(raw)
        raw += b" " * (case["pad_to"] - len(raw))
    transform = case.get("transform")
    if transform == "no_lf":
        raw = raw[:-1]
    elif transform == "extra_lf":
        raw += b"\n"
    elif transform == "crlf":
        raw = raw[:-1] + b"\r\n"
    elif transform == "nul":
        raw = raw[:-1] + b"\0\n"
    elif transform == "extra_field":
        raw = raw[:-1] + b"\textra\n"
    return raw


class ContractTests(unittest.TestCase):
    def outcome(self, expected, call, *args, **kwargs):
        if expected == "ok":
            return call(*args, **kwargs)
        with self.assertRaises(ContractError) as caught:
            call(*args, **kwargs)
        self.assertEqual(caught.exception.code, expected)

    def test_shared_control_and_wire_vectors(self):
        ids = set()
        for case in load("vectors.json"):
            with self.subTest(case=case["id"]):
                self.assertNotIn(case["id"], ids)
                ids.add(case["id"])
                kind, raw = case["kind"], materialize(case)
                if kind == "prepared":
                    self.outcome(case["expected"], prepared, raw)
                elif kind == "receipt":
                    self.outcome(
                        case["expected"], receipt, raw, case["rc"], case.get("command", "commit")
                    )
                else:
                    self.outcome(case["expected"], validate, kind, raw)

    def test_schema_has_only_resolvable_local_references(self):
        Draft202012Validator.check_schema(SCHEMA)

        def visit(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    ref = value["$ref"]
                    self.assertTrue(ref.startswith("#/$defs/"))
                    self.assertIn(ref.removeprefix("#/$defs/"), SCHEMA["$defs"])
                for child in value.values():
                    visit(child)
            elif isinstance(value, list):
                for child in value:
                    visit(child)

        visit(SCHEMA)
        for kind, file in [
            ("descriptor", "descriptor.json"),
            ("state", "state.json"),
            ("closing", "closing.json"),
        ]:
            self.assertTrue(Draft202012Validator(SCHEMA).is_valid(load(file)))
            validate(kind, (CASES / file).read_bytes())

    def test_all_byte_budgets_accept_exact_and_reject_plus_one(self):
        # Budget and grammar are distinct. Some frame maxima are deliberate
        # headroom, unreachable by a semantically valid frame (see README).
        for kind, maximum in LIMITS.items():
            with self.subTest(kind=kind):
                bounded(kind, b"x" * maximum)
                self.outcome("limit_exceeded", bounded, kind, b"x" * (maximum + 1))
        raw = encoded(load("event.json"))
        exact = raw + b" " * (65536 - len(raw))
        legacy.validate("event", exact)
        self.outcome("limit_exceeded", legacy.validate, "event", exact + b" ")

    def test_latches_are_empty_bytes_and_not_json_or_roles(self):
        for kind in ("disabled", "diagnostic"):
            latch(kind, b"")
            self.outcome("limit_exceeded", latch, kind, b"\n")
            self.outcome("limit_exceeded", latch, kind, b"{}")
        self.outcome("invalid_value", latch, "state", b"")

    def test_reference_and_utf8_path_boundaries(self):
        descriptor = load("descriptor.json")
        ref = (
            "sledra-ce1:"
            + descriptor["runtime_path"]
            + "/"
            + descriptor["session_id"]
            + "/event-context.json"
        )
        self.assertEqual(reference(ref.encode()), ref)
        for change in [
            ref.replace("sledra-ce1:", "sledra-ce2:"),
            ref + "/",
            ref.replace("/event-context", "/../event-context"),
            ref.replace("/node-local", "//node-local"),
            ref + "\n",
        ]:
            with self.subTest(value=change):
                self.outcome("invalid_reference", reference, change.encode())
        path = "/" + "雨" * 341
        self.assertEqual(len(path.encode()), 1024)
        absolute(path)
        self.outcome("limit_exceeded", absolute, path + "x")
        self.outcome("limit_exceeded", reference, b"x" * 2049)

    def test_prepared_exact_event_bytes_and_base64_consistency(self):
        event = prepared((CASES / "event.prepared.tsv").read_bytes())
        self.assertEqual(event["attributes"]["forecast_lead_hours"], 0)
        state = validate("state", (CASES / "writing.json").read_bytes())
        pending = state["pending"]
        record = base64.b64decode(pending["record_base64"])
        self.assertEqual(hashlib.sha256(record).hexdigest(), pending["record_sha256"])
        self.assertEqual(legacy.validate("event", record), event)
        # A base64 decoder accepting noncanonical pad bits must not widen CE-1.
        bad = copy.deepcopy(state)
        bad["pending"]["record_base64"] += "\n"
        self.outcome("invalid_value", validate, "state", encoded(bad))

    def test_role_vectors_and_reserved_names(self):
        for case in load("roles.json"):
            with self.subTest(case=case["id"]):
                self.outcome(case["expected"], authorize, **case["input"])

    def test_admission_recovery_diagnostics_and_compatibility_vectors(self):
        for file, check in [
            ("admission.json", admission),
            ("recovery.json", recovery),
            ("diagnostics.json", diagnostic),
            ("compatibility.json", compatibility),
        ]:
            for case in load(file):
                with self.subTest(file=file, case=case["id"]):
                    self.assertEqual(check(case["input"]), case["expected"])

    def test_typed_values_use_original_codec_without_loss_or_eval(self):
        for case in load("typed-values.json"):
            with self.subTest(case=case["id"]):
                result = self.outcome(case["expected"], typed_values, case["args"])
                if case["expected"] != "ok":
                    continue
                for key, lexeme in case.get("lexeme", {}).items():
                    self.assertEqual(str(result[key]), lexeme)
                event = load("event.json")
                # json.dumps cannot serialize Decimal. Preserve its lexeme in
                # the raw event exactly, instead of rounding through float.
                attrs = ",".join(
                    json.dumps(k)
                    + ":"
                    + (
                        str(v)
                        if isinstance(v, legacy.Decimal)
                        else json.dumps(v, ensure_ascii=False)
                    )
                    for k, v in result.items()
                )
                raw = encoded(event).decode()
                prefix = raw[: raw.index('"attributes":')] + '"attributes":{' + attrs + "}}"
                accepted = legacy.validate("event", prefix.encode())
                self.assertTrue(legacy.equivalent(accepted["attributes"], result))

    def test_capability_and_version_axes_are_independent(self):
        capabilities(b"CE-1\texecution-child-events\t1\n")
        for raw in (
            b"2.0.0\n",
            b"dev\n",
            b"CE-1\texecution-child-events\t2\n",
            b"CE-1\texecution-child-events\t1",
            b"CE-1\texecution-child-events\t1\nextra\n",
        ):
            self.outcome("protocol_mismatch", capabilities, raw)
        versions = json.loads((SPEC / "versions.json").read_bytes())
        self.assertEqual((versions["execution_format"], versions["execution_layout"]), ("2.0.0", 2))
        self.assertEqual(versions["legacy_exit_codes"], [0, 2, 3])
        self.assertTrue(
            set(versions["legacy_exit_codes"][1:]).isdisjoint(versions["ce_exit_codes"])
        )

    def test_unchanged_v2_checker_and_python_reader_accept_new_synthetic_stream(self):
        raw = (CASES / "child-events.jsonl").read_bytes()
        self.assertEqual(legacy.stream_conflicts(raw), [])
        events = [legacy.validate("event", line) for line in raw.splitlines()]
        config = load("root.json")
        with tempfile.TemporaryDirectory(prefix="sledra-ce-contract-") as work:
            root = Path(work) / "records"
            root.mkdir()
            (root / "sledra.json").write_bytes(encoded(config))
            first = events[0]
            path = root / legacy.execution_path(first["execution_id"], first["occurred_at"], config)
            path.parent.mkdir(parents=True)
            path.write_bytes(raw)
            control = Path(work) / "runtime"
            control.mkdir()
            for file in ("descriptor.json", "state.json", "closing.json"):
                (control / file).write_bytes((CASES / file).read_bytes())
            before = {str(p): p.read_bytes() for p in Path(work).rglob("*") if p.is_file()}
            with FileReader(root) as reader:
                rows = list(reader.iter_events(None, None))
            self.assertTrue(all(isinstance(row, Fact) for row in rows))
            self.assertEqual([row.data for row in rows], events)
            self.assertEqual(
                [
                    e["attributes"]["forecast_lead_hours"]
                    for e in events
                    if e["name"] == "gfs.modelvar.ready"
                ],
                [0, 3, 6, 9, 12],
            )
            self.assertEqual(events[1]["attributes"]["parameters"]["exact_steps"], 9007199254740993)
            self.assertEqual(
                {str(p): p.read_bytes() for p in Path(work).rglob("*") if p.is_file()}, before
            )

    def test_descriptor_state_and_closing_bind_to_one_reference(self):
        desc = (CASES / "descriptor.json").read_bytes()
        state = (CASES / "state.json").read_bytes()
        closing = (CASES / "closing.json").read_bytes()
        descriptor = load("descriptor.json")
        ref = (
            "sledra-ce1:"
            + descriptor["runtime_path"]
            + "/"
            + descriptor["session_id"]
            + "/event-context.json"
        ).encode()
        binding(desc, state, ref, closing)
        for field in ("session_id", "execution_id", "descriptor_sha256"):
            wrong = load("state.json")
            wrong[field] = (
                "0" * 64 if field.endswith("sha256") else "99999999-9999-4999-8999-999999999999"
            )
            self.outcome("identity_mismatch", binding, desc, encoded(wrong), ref)
        # Even semantically equivalent descriptor whitespace changes the immutable bytes.
        self.outcome("identity_mismatch", binding, desc + b" ", state, ref)
        wrong = load("closing.json")
        wrong["session_id"] = "99999999-9999-4999-8999-999999999999"
        self.outcome("identity_mismatch", binding, desc, state, ref, encoded(wrong))

    def test_argv_budget_counts_utf8_and_each_nul_separator(self):
        argv_payload(["x" * 131071])
        self.outcome("limit_exceeded", argv_payload, ["x" * 131072])
        argv_payload(["雨" * 43690, ""])
        self.outcome("limit_exceeded", argv_payload, ["雨" * 43690, "xx"])
        self.outcome("invalid_value", argv_payload, ["bad\0value"])

    def test_source_mapping_and_vector_inventory(self):
        source = json.loads((SPEC / "source-map.json").read_bytes())
        self.assertEqual(
            [m["difference"] for m in source["mappings"]], [f"D{i:02}" for i in range(1, 13)]
        )
        for mapping in source["mappings"]:
            self.assertIn("## " + mapping["old_heading"], (ROOT / mapping["old_file"]).read_text())
            self.assertIn(
                "## " + mapping["section"] + " ", (SPEC / mapping["new_file"]).read_text()
            )
        for file in (
            "vectors.json",
            "roles.json",
            "admission.json",
            "recovery.json",
            "diagnostics.json",
            "compatibility.json",
            "typed-values.json",
        ):
            cases = load(file)
            self.assertEqual(len(cases), len({c["id"] for c in cases}))

    def test_frozen_sources_stay_byte_identical(self):
        frozen = json.loads((SPEC / "frozen-inputs.json").read_bytes())
        for name, digest in frozen.items():
            with self.subTest(file=name):
                self.assertEqual(hashlib.sha256((ROOT / name).read_bytes()).hexdigest(), digest)
