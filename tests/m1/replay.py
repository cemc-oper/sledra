"""Restore and validate the frozen M1 evidence bundle without an analyzer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath

from verify import command, inspect, load, sha
from scripts.test_session import TestSession, add_arguments
import verify


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("bundle", type=Path)
    add_arguments(parser)
    args = parser.parse_args()
    with TestSession(args, "replay") as session:
        verify.SESSION = session
        bundle = json.loads(args.bundle.read_text())
        assert bundle["kind"] == "m1_handoff_fixture" and bundle["schema_version"] == "1.0.0"
        session.data["bundle_sha256"] = sha(args.bundle.read_bytes())
        root = session.work / "observability"
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
        binary = session.binary
        env = dict(session.env, SLEDRA_ROOT=str(root))
        for context in (root / "contexts").glob("*.json"):
            command(binary, env, "context", "validate", "--file", str(context))
        normal = load(root / "contexts/normal-run.json")
        retry = load(root / "contexts/retry-run.json")
        attempts = {
            name: load(root / "contexts" / f"{name}-attempt.json")
            for name in ("normal", "failed", "retry", "late")
        }
        summary = inspect(root, normal, retry, attempts)
        for key in (
            "run_ids",
            "attempt_ids",
            "process_event_types",
            "registry_outbox_types",
            "finish_covered_attempt_ids",
        ):
            assert summary[key] == bundle["summary"][key], key
        session.data.update(
            sample_count=len(restored),
            validated_contexts=6,
            process_events=11,
            recorder_root=str(root),
        )
        print(
            json.dumps(
                {
                    "ok": True,
                    "validated_contexts": 6,
                    "restored_files": len(restored),
                    "process_events": 11,
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
