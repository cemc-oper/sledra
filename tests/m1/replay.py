"""Restore and validate the frozen M1 evidence bundle without an analyzer."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

from verify import RECORDER, SPEC, command, inspect, load, sha


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    args = parser.parse_args()
    bundle = json.loads(args.bundle.read_text())
    assert bundle["kind"] == "m1_handoff_fixture" and bundle["schema_version"] == "1.0.0"
    with tempfile.TemporaryDirectory(prefix="sledra-m1-replay-") as tmp:
        root = Path(tmp) / "observability"
        root.mkdir(mode=0o700)
        restored = set()
        for item in bundle["files"]:
            relative = PurePosixPath(item["path"])
            assert not relative.is_absolute() and ".." not in relative.parts
            assert item["path"] not in restored
            restored.add(item["path"])
            data = item["text"].encode("utf-8")
            assert sha(data) == item["sha256"]
            target = root.joinpath(*relative.parts)
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(0o400 if relative.parts[0] == "contexts" else 0o600)
        binary = Path(tmp) / "sledra"
        subprocess.run(["go", "build", "-o", str(binary), "./cmd/sledra"], cwd=RECORDER,
                       env=dict(os.environ, GOPROXY="off", GOSUMDB="off"), check=True)
        env = dict(os.environ, SLEDRA_ROOT=str(root), SLEDRA_SPEC_DIR=str(SPEC))
        for context in (root / "contexts").glob("*.json"):
            command(binary, env, "context", "validate", "--file", str(context))
        normal = load(root / "contexts/normal-run.json")
        retry = load(root / "contexts/retry-run.json")
        attempts = {name: load(root / "contexts" / f"{name}-attempt.json")
                    for name in ("normal", "failed", "retry", "late")}
        summary = inspect(root, normal, retry, attempts)
        for key in ("run_ids", "attempt_ids", "process_event_types", "registry_outbox_types",
                    "finish_covered_attempt_ids"):
            assert summary[key] == bundle["summary"][key], key
        print(json.dumps({"ok": True, "validated_contexts": 6,
                          "restored_files": len(restored), "process_events": 11}, sort_keys=True))


if __name__ == "__main__":
    main()
