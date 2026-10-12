# Sledra source quickstart

Build the Go recorder and run a synthetic Bash owner locally. The example writes only to a fresh test directory and executes a synthetic payload once.

## Prepare dependencies

Use Linux/WSL with Bash, Go and Python 3. The module declares Go 1.25.0; the tested local toolchain is Go 1.27.0, Python 3.12.3 and Bash 5.2.21. Tests also require `jsonschema` from [requirements-test.txt](../spec/requirements-test.txt). This stage has no locked root setup command.

From the repository root, these preparation commands may access the network:

```bash
(cd recorder && go mod download)
python3 -m venv .venv
.venv/bin/python -m pip install -r spec/requirements-test.txt
```

Existing environments with these dependencies can skip preparation. Offline builds require the populated Go module cache. The recorder and Bash example themselves do not require Python.

## Build and run

```bash
repo_dir=$(pwd -P)
demo_dir=$(mktemp -d "${TMPDIR:-/tmp}/sledra-demo.XXXXXX")
(cd recorder && GOPROXY=off GOSUMDB=off go build -o "$demo_dir/sledra" ./cmd/sledra)
mkdir "$demo_dir/observability"
STANDALONE_WORK_DIR="$demo_dir" SLEDRA_ROOT="$demo_dir/observability" \
  SLEDRA_BIN="$demo_dir/sledra" SLEDRA_ENABLED=1 \
  "$repo_dir/examples/standalone/run.sh"
printf 'Demo files: %s\n' "$demo_dir"
```

The example locates its checked-in resources from the script path. Direct recorder calls should set absolute `SLEDRA_SPEC_DIR` and `SLEDRA_CONFIG` as described in [configuration](configuration.md).

`product.txt`, `executions.log` and `callbacks.log` show the synthetic business result and single payload invocation. Under `observability/`, `contexts/` contains Run/Attempt JSON, `registry/` holds immutable authority records and registration outbox events, `spool/` contains JSONL process/stage events, and `finish/` records the owner's explicit completion declaration. Recording these facts does not produce an analysis result.

## Verify normal execution and retry

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tests/m1/verify.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python tests/m1/replay.py tests/m1/results/handoff.json
```

The first command creates an isolated normal Run and a failure/retry Run with distinct Attempt identities and `retry_of`. It compares business output, exit code, product and invocation count to observation-disabled runs. The second replays the frozen snapshot and validates its hashes and contexts. Neither command rewrites committed evidence. See [local testing](testing-local.md) for the full regression.
