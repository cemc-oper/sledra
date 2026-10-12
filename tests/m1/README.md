# M1 local stage handoff

Run from the Sledra repository root after `make setup`. The unified entries are `make test` (regression and frozen replay), `make demo` (new handoff and replay) and `make benchmark` (new raw timing report); see [development](../../docs/development.md). Individual commands below also remain available with Go and a Python environment containing the locked test dependency. These scripts use only synthetic inputs, temporary private directories and the local development filesystem; they do not submit jobs, start an analyzer, contact a database or delete any existing spool segment.

```sh
PYTHONDONTWRITEBYTECODE=1 python3 tests/m1/verify.py
PYTHONDONTWRITEBYTECODE=1 python3 tests/m1/replay.py tests/m1/results/handoff.json
PYTHONDONTWRITEBYTECODE=1 python3 tests/m1/benchmark.py --samples 20
```

`verify.py` reuses the tool selected by `SLEDRA_TEST_BIN_DIR` during unified runs, or builds a private recorder offline when invoked alone. It creates one normal Run and one Run whose first Attempt fails and second Attempt succeeds with `retry_of`. It checks the synthetic payload's stdout, stderr, exit code, product and single invocation against a no-observation baseline. Each Run has an immutable finish declaration. A later registered-only Attempt is added after the retry Run's finish to prove that the old declaration is unchanged; the resulting `needs_finalization` projection belongs to M2. The script checks exact Registry outbox and process event types, context parent IDs, complete JSONL lines and fixed-path contexts.

[`results/handoff.json`](results/handoff.json) is a frozen, portable snapshot of the synthetic Registry, context, document, finish and spool JSON/JSONL files. Every entry carries its relative path, SHA-256 and original UTF-8 bytes; the bundle also gives expected Run/Attempt identities and a reference to a conflicting-event fixture. `replay.py` checks every digest, restores the hierarchy in a fresh private directory, validates all six contexts with the current recorder and rechecks the 11 process events and eight outbox events. The open segments deliberately have no real ingestion acknowledgment. The bundle is a raw input for future SLD-10/11/12/13 tests, not proof that those components already exist.

`benchmark.py` alternates enabled/disabled ordering across fresh local directories and asserts byte-for-byte business output equality in every pair. Its committed [`results/overhead-local.json`](results/overhead-local.json) records 20 raw nanosecond samples and p50/p95/p99/max for full Shell runs, paired added duration, Run/Attempt registration and one-shot event CLI calls. The first event call is recorded separately. These measurements contain Go process startup and local fsync costs. They are single-run local data, not A6-approved latency limits or shared/HPC filesystem qualification; CPU/RSS, disk-full behavior and multi-node lock latency remain to be measured before deployment.

Only after a maintainer-requested review described in the [engineering handoff](../../docs/engineering-handoff.md), refresh frozen artifacts deliberately using `--handoff-out tests/m1/results/handoff.json` and `--out tests/m1/results/overhead-local.json`, then update their hashes in the M1 closure evidence. `verify.py` generates fresh UUIDs and timestamps, so refreshing the handoff bundle changes its checksum even when its assertions remain identical.

M1 verification, replay and timing accept the [common source/installed test parameters](../../docs/testing-hpc.md). Reports identify the actual work filesystem; fixture platform/deployment IDs remain synthetic input identities, not assertions about the execution node.
