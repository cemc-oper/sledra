# Site test templates

`site.example.json` is an input template, not a verified site configuration. Copy it to a new user-owned file and supply the scheduler, partition, account, Python, installation/test-home paths, explicit shared work parent and report parent. This implementation supports Slurm; another scheduler needs a site adapter. All paths must exist and must be identically visible on both compute nodes. Python >=3.12, Bash >=5.2 and basic example utilities are test dependencies; Go is unnecessary with an explicit recorder binary.

```bash
python3 examples/hpc/site.py --config /absolute/path/to/site.json
```

Preparation creates a unique report directory, a unique shared test child and a rendered `job.sh`. It prints the submission and collection commands; it does not submit. Review the generated job before running the printed `sbatch` command in the site's test window. The job runs two roles with one task per node, bounded barriers and a configured timeout. Collect after completion even if the job failed. Missing nodes return nonzero and retain the prepared tree and available results. Keep scheduler job ID, allocation/node information, logs and archive checksum beside the report.

For a single node, invoke `tests/platform/probe.py`, `tests/m1/verify.py`, `tests/shell/verify.py` and optionally `tests/m1/benchmark.py` with `--mode hpc --bin ... --work-root ... --report-dir ... --keep-work`. Each report directory must be new. Run these on a compute node, not the login node, according to site policy. Details and the manual two-role procedure are in the repository `docs/testing-hpc.md` or the installed copy under `share/sledra/docs/`.

No template, login-node run or local simulation certifies shared storage, scheduling, MPI, signals, power-loss durability or an A6 performance budget.
