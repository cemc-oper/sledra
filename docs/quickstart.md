# Sledra source quickstart

Use the Go recorder and a synthetic Bash payload locally. Install the [supported development toolchains](development.md#toolchains) before starting. Source preparation uses the network; the daily commands use the populated caches offline.

## Prepare dependencies

From the repository root:

```bash
make help
make setup
```

Setup synchronizes the locked Python developer tools and downloads pinned Go modules. It works with empty dependency caches when the installed toolchains and network are available. Python is a development/verification dependency; recording requires the binary and Bash.

## Build and verify

```bash
make build
make check
make test
make demo
```

Build outputs are in `build/bin/`. Test/check/demo logs and summaries are in unique directories under `test-results/`. The demo creates isolated normal and failure/retry Runs, compares business output and single payload execution to observation-disabled runs, exports a fresh snapshot and replays it. The frozen compatibility input stays unchanged.

## Run the Bash owner example

For a manual successful owner-hook example after `make build`:

```bash
repo_dir=$(pwd -P)
demo_dir=$(mktemp -d "${TMPDIR:-/tmp}/sledra-demo.XXXXXX")
mkdir "$demo_dir/observability"
STANDALONE_WORK_DIR="$demo_dir" SLEDRA_ROOT="$demo_dir/observability" \
  SLEDRA_BIN="$repo_dir/build/bin/sledra" SLEDRA_ENABLED=1 \
  "$repo_dir/examples/standalone/run.sh"
printf 'Demo files: %s\n' "$demo_dir"
```

The example locates resources relative to the script. `product.txt`, `executions.log` and `callbacks.log` show the business output and one payload invocation. Under `observability/`, `contexts/`, `registry/`, `spool/` and `finish/` contain raw recording facts; no analysis result is produced. Each manual example uses a fresh directory. Direct recorder calls use embedded default contracts and explicit absolute runtime/input paths as described in [configuration](configuration.md).

See [local testing](testing-local.md) for individual checks and [development](development.md) for reports and dependency modes. See [CLI help and version](cli.md) for portable recording. For a source-independent trial, follow [installation](installation.md) and run `make package` / `make test-package`.
