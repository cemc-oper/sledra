"""Prepare a reviewable Slurm job; submission and collection are explicit steps."""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    cfg = json.loads(args.config.read_text())
    if cfg["scheduler"] != "slurm":
        raise ValueError(
            "this template supports Slurm only; other schedulers require a site adapter"
        )
    for name in ("partition", "account"):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", cfg[name]) or "REPLACE" in cfg[name]:
            raise ValueError(f"configure a concrete safe site {name}")
    if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", cfg["time_limit"]):
        raise ValueError("time_limit must be HH:MM:SS")
    for name in ("python", "test_home", "binary", "spec_dir", "work_root", "output_parent"):
        path = Path(cfg[name])
        if not path.is_absolute() or "\n" in str(path) or "\r" in str(path):
            raise ValueError(f"{name} must be an absolute path without newlines")
        path.resolve(strict=True)
    output = Path(cfg["output_parent"]) / ("sledra-site-" + uuid.uuid4().hex)
    output.mkdir()
    # Only preparation runs here; neither sbatch nor srun is invoked.
    command = [
        cfg["python"],
        str(Path(cfg["test_home"]) / "tests/platform/cross_node.py"),
        "prepare",
        "--mode",
        "hpc",
        "--bin",
        cfg["binary"],
        "--spec-dir",
        cfg["spec_dir"],
        "--work-root",
        cfg["work_root"],
        "--report-dir",
        str(output / "prepare"),
        "--samples",
        str(cfg["samples"]),
        "--timeout",
        str(cfg["timeout"]),
    ]
    with (output / "prepare.log").open("w") as log:
        result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
    if result.returncode:
        print(f"Preparation failed; inspect {output / 'prepare.log'}", file=sys.stderr)
        return result.returncode
    prepared = json.loads((output / "prepare/summary.json").read_text())
    replacements = {
        "PARTITION": cfg["partition"],
        "ACCOUNT": cfg["account"],
        "TIME_LIMIT": cfg["time_limit"],
        "OUTPUT": str(output / "slurm-%j.log"),
        "NODE_SCRIPT": str(Path(cfg["test_home"]) / "examples/hpc/node-role.sh"),
        "PYTHON": cfg["python"],
        "TEST_HOME": cfg["test_home"],
        "RUN_DIR": prepared["prepared_run"],
    }
    job = (HERE / "slurm.job.in").read_text()
    for key, value in replacements.items():
        job = job.replace(f"@{key}@", shlex.quote(value))
    (output / "job.sh").write_text(job)
    (output / "site.json").write_text(json.dumps(cfg, sort_keys=True, indent=2) + "\n")
    collection = [
        cfg["python"],
        str(Path(cfg["test_home"]) / "tests/platform/cross_node.py"),
        "collect",
        "--run-dir",
        prepared["prepared_run"],
        "--report-dir",
        str(output / "collection"),
        "--timeout",
        str(cfg["timeout"]),
    ]
    print(f"Prepared: {output}")
    print("Review job.sh and site.json; submit only in an approved site test window:")
    print(shlex.join(["sbatch", str(output / "job.sh")]))
    print("After the scheduler job finishes, collect:")
    print(shlex.join(collection))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
