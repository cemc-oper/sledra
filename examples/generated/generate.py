"""Generate the SLD-18 synthetic workflow with takflow's SLD-17 API."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import takflow
from takflow.backends.ecflow import EcflowBackend
from takflow.config import ObservabilityConfig
from takflow.flow import WorkflowEngine
from takflow.toolkit.observability import (
    TaskMapping,
    export_definition_manifest,
    write_definition_manifest,
)


HERE = Path(__file__).resolve().parent
TASKS = ("prepare", "forecast", "verify")
PARAMETERS = {
    "member": {"type": "string", "required": True},
    "lead_seconds": {"type": "integer", "required": True},
    "domain": {"type": "string", "required": False, "default": "global"},
}


def generate(output: Path, spec_dir: Path) -> dict:
    engine = WorkflowEngine(EcflowBackend())
    suite = engine.Suite("synthetic")
    main = suite.add_family("main")
    prepare = main.add_task("prepare")
    forecast = main.add_task("forecast")
    verify = main.add_task("verify")
    forecast.add_trigger("prepare == complete")
    verify.add_trigger("forecast == complete")
    mappings = [
        TaskMapping(prepare, "main.prepare", "prepare", "prepare"),
        TaskMapping(
            forecast, "main.forecast", "forecast", "forecast", PARAMETERS, ("main.prepare",)
        ),
        TaskMapping(verify, "main.verify", "verify", "verify", PARAMETERS, ("main.forecast",)),
    ]
    manifest = export_definition_manifest(
        suite,
        mappings,
        observability=ObservabilityConfig(enabled=True, spec_dir=str(spec_dir)),
        application_revision="sld18-synthetic-1",
        config_revision="sld18-synthetic-1",
        mapping_version="sld18-1",
        generator_version=takflow.__version__,
        script_invoke_mode="external",
    )
    assert manifest is not None
    output.mkdir(parents=True, exist_ok=True)
    engine.save_suite(suite, output / "synthetic.def")
    write_definition_manifest(output / "definition_manifest.json", manifest)
    (output / "expected_product.json").write_text(
        json.dumps({"path": "forecast.txt", "content": "synthetic forecast\n"}) + "\n"
    )
    jobs = output / "jobs"
    jobs.mkdir(exist_ok=True)
    for name in (
        "task.sh",
        "payload.sh",
        "verify.py",
        "stage-start.json",
        "stage-end-success.json",
        "stage-end-failure.json",
    ):
        shutil.copy2(HERE / name, jobs / name)
    for name in ("task.sh", "payload.sh"):
        (jobs / name).chmod(0o755)
    for task in TASKS:
        path = jobs / f"{task}.sh"
        path.write_text(
            '#!/usr/bin/env bash\nexec "$(dirname -- "$0")/task.sh" ' + task + ' "$@"\n'
        )
        path.chmod(0o755)
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--spec-dir", type=Path, required=True)
    args = parser.parse_args()
    generate(args.output.resolve(), args.spec_dir.resolve())


if __name__ == "__main__":
    main()
