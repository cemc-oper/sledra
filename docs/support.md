# Supported use and validation matrix

Current evidence covers synthetic recorder usage and engineering tooling. Each row identifies what was exercised; requirements and cross-builds do not imply tested compatibility. Contact repository maintainers through [Issues](https://github.com/cemc-oper/sledra/issues). No published-release support SLA is declared.

| Environment or capability | Status | Evidence and limits |
|---|---|---|
| WSL2 Linux x86_64, ext4; Go 1.27.0, Python 3.12.3, Bash 5.2.21, uv 0.11.2, Make 4.3 | Local pass | Source checks, recorder/contract/Shell/M1 regression and shared entry points |
| Linux amd64 static trial, read-only installation with spaces, no source/Go/cache or runtime network | Local pass | Namespace package acceptance, raw M1 facts, Shell/primitive/harness/timing tools |
| Explicit `/dev/shm` tmpfs work root | Local pass | Ten synthetic timing pairs and actual mount report; no A6 threshold approval |
| Two role processes on one WSL host | Harness pass | Concurrent registration, same absolute context, shared spool and flock barriers; real cross-node unverified |
| Linux arm64 | Cross-build only | Static ELF inspected and wrong-host preflight rejected; native runtime unverified |
| Other permitted toolchain versions | Unverified | Development floors are Python 3.12, Go 1.25.0, Bash 5.2, uv 0.11.2 and Make 4.0; not a tested version matrix |
| GitHub-hosted Ubuntu 24.04 amd64 workflow | Static/local command validation | Actual remote run, caches, upload and runner namespace configuration unverified |
| Real compute nodes/shared HPC filesystems and scheduler behavior | Unverified | Explicit test roots and Slurm templates exist; site parameters/window and SLD-03/25 evidence are required |
| Signals, MPI and transparent supervisor | Limited/unverified | Only existing local owner-hook/Bash profiles are tested; INT immediate handling and `sledra exec` are not qualified |
| A6 performance, power-loss/storage durability and network faults | Unverified | Raw local timing/process-exit visibility cannot establish these claims |
| Public release/licensing/signing and full offline upgrade/rollback | Pending | A7 license/contribution approval and SLD-26 qualification remain separate |
| Analysis, queries and real ingestion acknowledgements | Not implemented | M2 work; isolated retention fixtures are not a real collector |

The [development guide](development.md) records minimum/tested tools. [Installation](installation.md) describes artifact identity and runtime dependencies. [HPC testing](testing-hpc.md) records commands and node/mount scope. The [engineering handoff](engineering-handoff.md) preserves compatibility inputs and remaining stage branches.
