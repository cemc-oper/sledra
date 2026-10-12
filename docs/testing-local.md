# Sledra local regression

Prepare dependencies once using the explicit online setup, then use the offline public commands from the repository root:

```bash
make setup
make check
make test
```

See [development](development.md) for minimum/tested toolchains, dependency-cache requirements, build reuse and report locations. `make check` validates format/static checks, locked metadata, generated contract/fixture bytes, local documentation links and workflow configuration with the setup-prepared actionlint. Generation happens in a temporary copy. `make test` builds or reuses each Go helper once and executes Go, engineering, package integrity/archive unit tests, contract/event/context, portable CLI, Shell, M1 normal/failure/retry and frozen replay tests.

Every check/test run has a unique ignored `test-results/` directory with stage logs and `summary.json`. A failure returns nonzero and identifies the failing stage. The report compares all protected fixtures before and after the run; ordinary tests never rewrite `tests/m1/results/` or `tests/platform/results/`.

## Individual checks

After setup, use the locked environment for a specific script. To reuse existing build tools across direct invocations:

```bash
make build
SLEDRA_TEST_BIN_DIR="$PWD/build/bin" uv run --locked --offline python spec/verify.py
SLEDRA_TEST_BIN_DIR="$PWD/build/bin" uv run --locked --offline python tests/cli/verify.py
SLEDRA_TEST_BIN_DIR="$PWD/build/bin" uv run --locked --offline python tests/shell/verify.py
SLEDRA_TEST_BIN_DIR="$PWD/build/bin" uv run --locked --offline python tests/m1/replay.py tests/m1/results/handoff.json
```

Without `SLEDRA_TEST_BIN_DIR`, an individual helper builds its tool in a private temporary directory using the prepared Go cache. An explicit missing tool fails. Existing `python3` invocations still work when that interpreter has jsonschema installed; the uv environment is the reproducible default.

## Package acceptance

`make package` builds the trial; `make test-package` verifies the existing artifact in source-free, Go-free, network-isolated namespaces with a read-only installation. It additionally proves recording without Python. See [installation](installation.md) for system isolation dependencies, artifact selection and retained reports. Cross-compiled targets remain unverified until run on matching hardware.

## Demo and performance

```bash
make demo
make benchmark
```

The [M1 guide](../tests/m1/README.md) describes the normal/failure/retry demonstration, frozen handoff and separate timing probe. Demo and benchmark write new report artifacts rather than replacing historical data. The [platform probe](../tests/platform/README.md) requires a writable test parent and establishes only local filesystem primitives; save new platform results separately. The common parameters, installed test tools and HPC report scope are described in [HPC testing](testing-hpc.md). `make test-platform` runs single-node primitives and the local two-role simulation; `make test` includes isolation/lost-worker/preflight regressions.

## Authoring source review

`make check` checks repository-local Markdown links. In the authoring workspace, additionally run:

```bash
uv run --locked --offline python scripts/check_docs.py --plan-dir ../../docs/observability-plan
```

An independent source copy can run all daily targets without that neighboring directory. The optional source review checks recorded upstream digests, as described in [contract sources](contracts.md).

The [CI workflow](ci.md) runs these same gates and preserves report artifacts. The [support matrix](support.md) records local acceptance separately from remote Actions and HPC qualification.
