"""Compare the offline Go event CLI with Python canonical 1.0 envelopes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from copy import deepcopy
from pathlib import Path

from check import ContractError, canonical, read_json, validate_bundle


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "analysis" / "src"))
from scripts.test_tools import binary_for  # noqa: E402
from sledra_analysis.ingest.validation import EventValidator  # noqa: E402
from sledra_analysis.store.canonical import RecordError  # noqa: E402



def main() -> None:
    with tempfile.TemporaryDirectory(prefix="sledra-event-") as scratch:
        temp = Path(scratch)
        binary = binary_for("sledra-event", temp)
        env = dict(os.environ, GOPROXY="off", GOSUMDB="off")
        count = 0
        for filename in ("valid-bundle.json", "valid-future-extension.json"):
            bundle = read_json(ROOT / "spec" / "fixtures" / "1.0.0" / filename)
            for event in bundle["events"]:
                (temp / "input.json").write_bytes(canonical(event))
                (temp / "config.json").write_text(
                    json.dumps({"producer_role": event["producer"]["role"]}),
                    encoding="utf-8")
                completed = subprocess.run(
                    [str(binary), "encode", "-config", str(temp / "config.json"),
                     "-input", str(temp / "input.json"), "-output-dir", str(temp)],
                    cwd=temp, env=env, capture_output=True, check=True)
                assert completed.stdout == b"" and completed.stderr == b""
                actual = (temp / (event["event_id"] + ".json")).read_bytes()
                assert actual == canonical(event) + b"\n", event["event_type"]
                count += 1
        malicious = dict(event)
        malicious["extensions"] = {"vendor.future": {"password": "do-not-print"}}
        (temp / "input.json").write_bytes(canonical(malicious))
        rejected = subprocess.run(
            [str(binary), "validate", "-config", str(temp / "config.json"),
             "-input", str(temp / "input.json")],
            cwd=temp, env=env, capture_output=True)
        assert rejected.returncode == 1 and rejected.stdout == b""
        assert rejected.stderr == b'{"code":"sensitive_field","ok":false}\n'
        print(f"Go/Python event envelope match: {count}/{count}")
        check_semantics(binary, temp, env)


def check_semantics(binary: Path, temp: Path, env: dict[str, str]) -> None:
    """Exercise one semantic vector through the codec, contract, and ingest validator."""
    bundle = read_json(ROOT / "spec" / "fixtures" / "1.0.0" / "valid-bundle.json")
    future_bundle = read_json(ROOT / "spec" / "fixtures" / "1.0.0" / "valid-future-extension.json")
    known = deepcopy(bundle["events"][5])
    future = deepcopy(future_bundle["events"][-1])
    vectors = [("known", known, "known"), ("future", future, "opaque")]
    future_payload = deepcopy(future)
    future_payload["payload"]["observation_time"] = "future-domain-label"
    vectors.append(("opaque payload remains open", future_payload, "opaque"))

    missing_known = deepcopy(known)
    del missing_known["association"]["task_instance_id"]
    vectors.append(("known missing parent", missing_known, "parent_mismatch"))
    missing_future = deepcopy(future)
    missing_future["association"]["attempt_id"] = known["association"]["attempt_id"]
    vectors.append(("future missing parent", missing_future, "parent_mismatch"))
    invalid_offset = deepcopy(known)
    invalid_offset["recorded_at"] = "2026-10-10T03:12:14+01:99"
    vectors.append(("invalid offset minute", invalid_offset, "schema_invalid"))
    invalid_hour = deepcopy(known)
    invalid_hour["occurred_at"] = "2026-10-10T03:12:14+24:00"
    vectors.append(("invalid offset hour", invalid_hour, "schema_invalid"))
    unknown_offset = deepcopy(known)
    unknown_offset["recorded_at"] = "2026-10-10T03:12:14-00:00"
    vectors.append(("unknown local offset", unknown_offset, "known"))
    engine = deepcopy(known)
    engine["event_type"] = "engine.state_observed"
    engine["producer"]["role"] = "engine_observer"
    engine["source"]["kind"] = "observed"
    engine["association"] = {"run_id": known["association"]["run_id"]}
    engine["payload"] = {
        "engine_instance": "engine-a",
        "node_path": "/suite/family/task",
        "raw_state": "complete",
        "observation_time": known["recorded_at"],
        "source_position": "opaque:synthetic-source",
    }
    vectors.append(("logical node path", engine, "known"))
    invalid_payload_time = deepcopy(engine)
    invalid_payload_time["payload"]["observation_time"] = "2026-10-10T03:12:14+01:99"
    vectors.append(("invalid payload offset", invalid_payload_time, "schema_invalid"))
    invalid_evidence_time = deepcopy(known)
    invalid_evidence_time["evidence_refs"] = [{
        "evidence_id": "synthetic-evidence-a",
        "locator": "opaque:synthetic-check-a",
        "source": "synthetic-checker",
        "availability": "present",
        "retention_until": "2026-10-10T03:12:14+01:99",
    }]
    vectors.append(("invalid evidence offset", invalid_evidence_time, "schema_invalid"))

    validator = EventValidator()
    mismatches = []
    for name, event, expected in vectors:
        raw = canonical(event)
        (temp / "input.json").write_bytes(raw)
        (temp / "config.json").write_text(
            json.dumps({"producer_role": event["producer"]["role"]}), encoding="utf-8"
        )
        go = subprocess.run(
            [str(binary), "validate", "-config", str(temp / "config.json"),
             "-input", str(temp / "input.json")],
            cwd=temp, env=env, capture_output=True, check=False,
        )
        go_result = "ok" if go.returncode == 0 else json.loads(go.stderr)["code"]
        trial = deepcopy(bundle)
        trial["events"] = [event]
        try:
            contract = validate_bundle(trial)
            contract_result = "opaque" if contract["opaque"] else "known"
        except ContractError as exc:
            contract_result = exc.code
        try:
            ingest_result = "known" if validator.validate(raw).supported else "opaque"
        except RecordError as exc:
            ingest_result = exc.code
        if (go_result, contract_result, ingest_result) != (
            "ok" if expected in ("known", "opaque") else expected,
            expected,
            expected,
        ):
            mismatches.append((name, expected, go_result, contract_result, ingest_result))
    assert not mismatches, mismatches
    print(f"Shared event semantic vectors: {len(vectors)}/{len(vectors)}")


if __name__ == "__main__":
    main()
