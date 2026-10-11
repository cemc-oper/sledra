# SLD-03 local platform probe

Run only on a writable test filesystem. The probe creates a uniquely named child of `--parent`, writes synthetic files there, and removes that child on exit. It does not call a scheduler or modify an existing file in `--parent`.

```bash
python3 tests/platform/probe.py --parent . --samples 64 > tests/platform/results/wsl2-ext4-2026-10-11.json
```

The checked-in result is from the WSL2 ext4 development volume. For another mount, choose an existing writable test parent on that mount and save a separate report. `--samples` accepts 10–1000; each 4096-byte latency sample is removed before the next. The probe tests hardlink create-if-absent, file and directory fsync, same-node contention, process-exit recovery visibility, advisory `flock`, a fixed absolute path read from another process, Bash signal traps and background `wait`, and a `CGO_ENABLED=0` static build of the **contract test helper**. It records latency distributions and asserts a test tree limit of 200 files and 10 MiB of logical file content. Go's build cache is outside that test tree and is not included in this limit.

`status: pass` applies only to the tested local mount and these synthetic operations. A process exit is not a power-loss durability test. Same-host contenders are not cross-node visibility or lock tests. The binary is not the future production recorder. Production recorder overhead, shared/HPC mounts, real storage quotas, network faults and deployment latency thresholds require separate evidence.
