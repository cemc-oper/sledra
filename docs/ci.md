# CI and local reproduction

The [GitHub Actions workflow](../.github/workflows/ci.yml) runs on main pushes, pull requests and manual dispatch. It uses Ubuntu 24.04 amd64 with pinned Python 3.12.3, Go 1.27.0 and uv 0.11.2. Action references are full commits. Local static validation and equivalent local commands have acceptance evidence; no remote Actions execution was performed in this task. The [support matrix](support.md) keeps the proposed runner separate from measured WSL behavior.

## Shared entry points

After installing toolchains and namespace test dependencies, run exactly the workflow's public commands from a source checkout:

```bash
make setup
make check
make test
make test-platform
make demo
make package
make test-package
```

Setup always runs, including cache hits. It populates locked Python/Go dependencies and the pinned actionlint validator. Go/uv cache restoration is optional; `workflow_dispatch` with `cold_cache=true` skips restoration and exercises setup with fresh runner cache paths. Setup can use the network; subsequent project checks disable dependency queries and workflow validation does not download tools. Real OS network isolation is proved specifically by test-package, not by the offline dependency flags alone.

On the disposable hosted runner only, the workflow installs bubblewrap/util-linux and allows unprivileged user namespaces when the AppArmor sysctl exists. This enables the actual source-free/read-only/network-isolated acceptance; missing support still fails. It does not change a local workstation or HPC policy. The package test separately requires system `/usr/bin/python3` >=3.12. No host-mode fallback replaces it.

`make check` also runs `scripts/check_ci.py`, using actionlint 1.7.11 and native archive hashes from [.github/ci-tools.json](../.github/ci-tools.json). Missing or corrupted validator caches fail with setup instructions. For an explicitly installed validator of that version, use `python3 scripts/check_ci.py --binary /absolute/path/to/actionlint`; the report identifies the override and its binary digest. Optional shellcheck/pyflakes integrations are disabled so availability does not change this gate; Bash syntax remains a separate shared check.

## Permissions and evidence

The workflow grants contents read only, disables persisted checkout credentials, uses no repository secrets and does not publish releases or execute HPC jobs. Stage failures stop the checks. An always-run upload step retains summaries/logs/commands/samples/fresh handoffs and build/package metadata for 14 days; it does not upload archive/executable files. Cache restore, upload, hosted-runner namespace setup and actual GitHub orchestration remain unverified until a remote run is recorded. A cancellation or failed runner bootstrap may prevent complete evidence collection.

Workflow/static validation is not an Actions run, and Ubuntu scheduling is not HPC qualification. Link the actual run and its environment to the acceptance record when it exists. No workflow dispatch, push or branch-protection setting was changed during local implementation.

## Updating CI inputs

Review the upstream [checkout](https://github.com/actions/checkout), [setup-python](https://github.com/actions/setup-python), [setup-go](https://github.com/actions/setup-go), [setup-uv](https://github.com/astral-sh/setup-uv), [upload-artifact](https://github.com/actions/upload-artifact) and [actionlint](https://github.com/rhysd/actionlint) projects before changing pins. Resolve release tags to full commits, review runtime/input changes, retain provenance and validate the rendered workflow with the pinned tool. For a validator update, replace both native archive checksums and version, run setup and rerun workflow negative cases. Toolchain changes need new evidence in the support matrix; selecting a version is not proof that it passed.
