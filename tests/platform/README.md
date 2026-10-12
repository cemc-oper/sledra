# Platform probes and node harness

See [common parameters and HPC workflow](../../docs/testing-hpc.md) for source/installed invocations, dependencies and scoped reports. `make test-platform` runs the single-node probe and the local two-role harness with an existing shared development binary. `make test` covers parallel isolation, owned cleanup, missing-node deadlines, foreign architecture and missing dependency reports.

`probe.py` still accepts `--parent` as a legacy alias for `--work-root`. Always use a new `--report-dir`; do not overwrite the [historical WSL2 result](results/wsl2-ext4-2026-10-11.json). Version 2 reports include raw filesystem latency samples, actual recorder ELF/version startup measurements and actual work mount information. The test tree remains bounded to 200 files and 10 MiB excluding any externally supplied recorder. Go cache costs are outside the test tree.

`cross_node.py` offers prepare, worker, collect and local simulate commands. Registry/context/spool share one newly owned test tree, with the same absolute context read by both roles. Atomic barriers have deadlines; results preserve role commands, nodes, mount metadata, registration timings, flock exclusion/release and spool visibility. Local simulation is not real cross-node evidence. Prepared/failed trees are retained; each role can be claimed only once.

A synthetic pass qualifies only the observed operations on the named mounts and nodes. Real shared/HPC storage, power-loss durability, scheduler signals, MPI and A6 budgets remain separate unverified branches until site evidence exists.
