# Sledra

Sledra records workflow Run and Attempt identities, immutable contexts and process events without coupling the business payload to an analysis service. The current local implementation provides a Go recorder, a thin Bash API and synthetic examples. Observation failures preserve the payload's output and exit behavior in the tested owner-hook profile.

## Current capabilities

The recorder provides atomic Run/Attempt registration, context validation, canonical event encoding, session JSONL spools, capacity guards and explicit Run finish declarations. A frozen [M1 handoff](tests/m1/README.md) exercises normal execution and failure followed by retry. The contract is a **1.0.0 development draft**.

Analysis, queries, real ingestion acknowledgements and a transparent `sledra exec` supervisor are future work. The retention helper is a [test fixture](recorder/retention/README.md), unsuitable for production cleanup.

## Components and deployment

| Directory | Purpose and placement |
|---|---|
| [recorder](recorder/) | Go module `github.com/cemc-oper/sledra/recorder`; build the `sledra` binary for the execution host |
| [shell](shell/README.md) | Bash functions sourced by a known lifecycle owner |
| [spec](spec/README.md) | Portable schemas, event catalog, canonical identity rules and synthetic vectors |
| [examples/standalone](examples/standalone/README.md) | Synthetic payload with explicit owner hooks |
| [tests](docs/testing-local.md) | Contract, Go, Shell, M1 and local filesystem checks |
| [docs](docs/README.md) | User and developer documentation usable from an independent source copy |

Install resources separately from runtime data. Registry, contexts, documents and spool files live under one explicit `SLEDRA_ROOT`. A shared/HPC deployment requires qualification of the actual target filesystem; current evidence covers local WSL/ext4 only. No `analysis/` package exists yet.

## Quick start

With Go, Bash and an existing Go module cache, run from this repository root:

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

The payload prints synthetic output and creates `product.txt`. Inspect `observability/contexts/`, `observability/registry/`, `observability/finish/` and `observability/spool/` under that directory. See [quickstart](docs/quickstart.md) for dependency preparation and the verified normal/failure/retry scenario. Each manual demo uses a fresh directory.

## Installation and testing

Source builds are currently supported; an installable trial package is planned in SLD-31. [Local testing](docs/testing-local.md) lists the existing regression commands and required dependencies. Unified Make targets, locked developer setup, CLI help/version and portable default resource lookup are planned in SLD-29/30; use the documented existing commands and explicit absolute resource paths today.

## Documentation and limits

Start with the [documentation index](docs/README.md), [architecture](docs/architecture.md), [configuration](docs/configuration.md) and [contract source policy](docs/contracts.md). The [name and path migration](docs/migration.md) explains old names in historical evidence.

Local passing tests do not establish HPC qualification, approved performance budgets or permission for a public release. License selection remains open. M1 recording is complete locally; M1.5 is preparing engineering and trial delivery, M2 will add offline analysis, M3 integration, and M4 business reports and delivery qualification.
