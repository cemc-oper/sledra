# Local and HPC test parameters

The same standard-library Python tools are available from source and from the Linux trial package. Set `TEST_HOME` to the source repository or to an installation's `share/sledra`; set `SLEDRA` to the existing native recorder. Run from any working directory. Test dependencies are Python >=3.12, Bash >=5.2 and dirname/cat/sleep; Shell assertions also use grep; the single-node primitives also require Linux flock/fsync/hardlinks, findmnt and basic Shell probe utilities. Explicit binaries require no Go compiler. A source invocation without `--bin` uses the shared development override or an offline standalone build.

| Parameter | Meaning |
|---|---|
| `--bin` | Existing native `sledra`; missing or foreign binaries fail without rebuilding |
| `--spec-dir` | Compatible contract override; defaults to resources beside the tools |
| `--work-root` | Existing parent on the filesystem under test; HPC requires an explicit value |
| `--report-dir` | New report directory; existing reports are never overwritten |
| `--keep-work` | Retain a successful unique work child; failures always retain it |
| `--mode local/hpc` | Test intent, not platform qualification; default local |

Each invocation creates only its own uniquely named child beneath the selected parent. Registry, contexts, spool, requests, baseline products and temporary files live there. Absolute context paths are recorded. Successful cleanup removes only that owned child; it preserves the parent, sibling runs and reports. Do not point tests at production data. Failed permission or missing-dependency preflights produce `summary.json` in the requested new report directory whenever it can be created. Invalid CLI syntax or an unwritable/existing report directory fail before a report can be reserved.

```bash
python3 "$TEST_HOME/tests/m1/verify.py" --bin "$SLEDRA" \
  --work-root "$TEST_PARENT" --report-dir "$REPORT_PARENT/m1-new" --keep-work
python3 "$TEST_HOME/tests/m1/replay.py" "$TEST_HOME/tests/m1/results/handoff.json" \
  --bin "$SLEDRA" --work-root "$TEST_PARENT" --report-dir "$REPORT_PARENT/replay-new"
python3 "$TEST_HOME/tests/shell/verify.py" --bin "$SLEDRA" \
  --work-root "$TEST_PARENT" --report-dir "$REPORT_PARENT/shell-new"
python3 "$TEST_HOME/tests/platform/probe.py" --bin "$SLEDRA" --samples 10 \
  --work-root "$TEST_PARENT" --report-dir "$REPORT_PARENT/primitives-new" --keep-work
python3 "$TEST_HOME/tests/m1/benchmark.py" --bin "$SLEDRA" --samples 10 \
  --work-root "$TEST_PARENT" --report-dir "$REPORT_PARENT/timing-new" --keep-work
```

For a compute-node single-node test add `--mode hpc`; there is no fallback to node `/tmp`. The original `probe.py --parent` remains an alias for `--work-root`. Reports record actual target mount information from Linux mountinfo, architecture, host, tool versions, source inventory digest or installed manifest digest, binary SHA256/version, test commands, sample counts and reasons. Benchmark and filesystem latency reports retain raw nanosecond samples and quantiles; A6 remains unverified without approved budgets. A process crash cannot prove power-loss durability. Retain the detached archive checksum separately because an extracted tree cannot reconstruct the original compressed archive.

## Two roles and shared storage

First exercise the harness locally:

```bash
python3 "$TEST_HOME/tests/platform/cross_node.py" simulate --bin "$SLEDRA" \
  --work-root "$TEST_PARENT" --report-dir "$REPORT_PARENT/simulation-new" \
  --samples 8 --timeout 60 --keep-work
```

This reports a local harness pass, with `real_cross_node: unverified`. `--omit-role b --timeout 1` deliberately exercises a lost-node failure. Concurrent invocations must use separate new report directories; their work children and identities are independent.

For real compute nodes, prepare with `cross_node.py prepare --mode hpc` and the common paths. Preparation retains the tree and prints `run_directory`. Launch one worker on each distinct node using the **same absolute prepared path**, native binary, spec and installation visible at the same paths:

```bash
python3 "$TEST_HOME/tests/platform/cross_node.py" worker --run-dir "$RUN_DIR" --role a
python3 "$TEST_HOME/tests/platform/cross_node.py" worker --run-dir "$RUN_DIR" --role b
```

After both workers exit, run collection even if one failed:

```bash
python3 "$TEST_HOME/tests/platform/cross_node.py" collect --run-dir "$RUN_DIR" \
  --report-dir "$REPORT_PARENT/collection-new" --timeout 60
```

Workers claim their roles exclusively, wait at bounded readiness/registration/lock barriers, concurrently repeat identical Run and Attempt registration, compare identities, validate the same absolute context and emit events into the shared spool. Role A holds an advisory lock; role B must observe exclusion and acquire after release. Collection checks both receipts, context bytes/path, exactly two Registry records and both process events. Every receipt includes node, mount, binary metadata, commands and timing samples. Collection retains the tree, includes partial receipts on failure and returns nonzero for missing/failed roles. Distinct node names and machine IDs in HPC mode can mark these particular cross-node operations pass; full HPC platform qualification remains separate. Preparation alone has `execution: unverified`.

The [site templates](../examples/hpc/README.md) separate prepare, review/submit and collect, with scheduler, partition and account supplied by configuration. Currently only Slurm has a rendered adapter. No job is submitted by the preparation tool. Retain scheduler allocation evidence to establish real compute-node scope. No real HPC window or site parameters were available for the development acceptance; real HPC remains unverified. SLD-03/25 own platform qualification; SLD-32 delivers tools and local harness evidence, not those task closures.
