"""Run the same 1.0 contract cases through Python and Go without network access."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from check import canonical, content_sha, length_digest, read_json, registry_id, sha, task_instance_id


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.test_tools import binary_for  # noqa: E402

FIXTURES = ROOT / "spec" / "fixtures" / "1.0.0"


def run(command: list[str], *, cwd: Path | None = None) -> tuple[int, dict]:
    env = {**os.environ, "GOPROXY": "off", "GOSUMDB": "off"}
    result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True, check=False)
    output = result.stdout if result.returncode == 0 else result.stderr
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError as exc:
        raise AssertionError(f"{command}: {result.returncode}: {output}") from exc
    return result.returncode, parsed


def check_vectors() -> None:
    vectors = read_json(FIXTURES / "golden-vectors.json")
    bundle = read_json(FIXTURES / "valid-bundle.json")
    params = bundle["attempt_context"]["parameters"]
    assert canonical(params).decode() == vectors["canonical_parameters"]
    assert task_instance_id(bundle["run_context"]["run_id"], "main.forecast", params) == vectors["task_instance_id"]
    assert registry_id("registry-synthetic-1", "run", "wsl-sandbox", "launch-coldstart-20261010") == vectors["run_id"]
    assert registry_id("registry-synthetic-1", "attempt", vectors["task_instance_id"], "exec-member01-first") == vectors["attempt_id"]
    job = bundle["job_ref"]
    assert "job_" + length_digest(job["platform_id"], job["scheduler"], job["cluster_id"],
                                  job["scheduler_job_id"], job["submission_instance"]) == vectors["job_ref_id"]
    assert "job_" + length_digest(job["platform_id"], job["scheduler"], job["cluster_id"],
                                  job["scheduler_job_id"], "submit-b") == vectors["reused_scheduler_job_id_ref"]
    assert vectors["job_ref_id"] != vectors["reused_scheduler_job_id_ref"]
    assert content_sha(bundle["definition_manifest"], ("content_sha256", "definition_id")) == vectors["manifest_sha256"]
    assert content_sha(bundle["run_context"], ("context_sha256",)) == vectors["run_context_sha256"]
    assert content_sha(bundle["attempt_context"], ("context_sha256",)) == vectors["attempt_context_sha256"]
    assert sha(bundle["events"][5]) == vectors["event_sha256"]
    assert canonical({"label": "e\u0301", "emoji": "☀"}).decode() == vectors["unicode_nfc"]
    assert canonical({"z": 1, "a": 2}).decode() == vectors["key_order"]
    assert canonical({"member": "01"}).decode() == vectors["string_01"]
    assert canonical({"member": 1}).decode() == vectors["integer_1"]
    assert vectors["string_01"] != vectors["integer_1"]
    assert vectors["zero_hour_seconds"] == 0
    assert canonical(json.loads('{"n":-0}')).decode() == vectors["negative_zero_canonical"]
    assert {item["value"] * (3600 if item["unit"] == "h" else 1) for item in vectors["lead_inputs"]} == {0}


def main() -> None:
    check_vectors()
    catalog = read_json(ROOT / "spec" / "1.0.0" / "event-types.json")
    capabilities = read_json(ROOT / "spec" / "1.0.0" / "capabilities.json")
    assert len(catalog) == 24
    assert sorted(catalog) == capabilities["known_event_types"]
    assert "task.success" not in catalog
    for line in (FIXTURES / "SHA256SUMS").read_text().splitlines():
        digest, name = line.split("  ", 1)
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest, name
    cases = read_json(FIXTURES / "cases.json")
    positive_results = {}
    with tempfile.TemporaryDirectory(prefix="sledra-spec-") as temp:
        binary = binary_for("sledra-spec-check", Path(temp))
        for name, expected in sorted(cases.items()):
            fixture = FIXTURES / name
            py_rc, py = run([sys.executable, str(ROOT / "spec" / "check.py"), str(fixture)])
            go_rc, go = run([str(binary), "-spec-dir", str(ROOT / "spec" / "1.0.0"), str(fixture)])
            assert (py_rc == 0) == (expected == "ok"), (name, py)
            assert (go_rc == 0) == (expected == "ok"), (name, go)
            if expected == "ok":
                assert py == go, (name, py, go)
                positive_results[name] = py
                if name in ("valid-future-extension.json", "valid-surrogate-escape.json"):
                    original = read_json(fixture)["events"][-1]
                    assert py["opaque_events"] == [original]
            else:
                assert py["code"] == go["code"] == expected, (name, py, go)
            print(f"{name}: {expected}")
    assert (positive_results["valid-bundle.json"]["bundle_canonical_sha256"] ==
            positive_results["valid-negative-zero.json"]["bundle_canonical_sha256"])
    assert (positive_results["valid-future-extension.json"]["bundle_canonical_sha256"] ==
            positive_results["valid-surrogate-escape.json"]["bundle_canonical_sha256"])
    for name in ("valid-bundle.json", "valid-future-extension.json", "golden-vectors.json"):
        print(f"sha256 {name}: {hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()}")


if __name__ == "__main__":
    main()
