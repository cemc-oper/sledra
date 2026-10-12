"""Compare the offline Go event CLI with Python canonical 1.0 envelopes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from check import canonical, read_json


ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.test_tools import binary_for  # noqa: E402



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


if __name__ == "__main__":
    main()
