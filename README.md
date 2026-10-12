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

Install the [development toolchains](docs/development.md#toolchains), then run from this repository root:

```bash
make setup
make build
make check
make test
make demo
```

Setup is the explicit network step and works with empty dependency caches. Daily targets disable dependency network queries and reuse `build/bin/` tools. The synthetic demo verifies normal execution and failure/retry, then exports and replays a fresh handoff in a unique `test-results/` directory. See [quickstart](docs/quickstart.md) for a manual Bash owner example and [local testing](docs/testing-local.md) for detailed regression commands.

## Installation and testing

Source builds and a Linux local trial archive are available. [Installation](docs/installation.md) covers `make package`, isolated `make test-package` and user-directory use. `make help` lists the current targets, dependencies and outputs. [Development](docs/development.md) explains locked setup, offline source builds, reports and the separate `make benchmark` measurement. The [CLI guide](docs/cli.md) covers environment-free help/version, embedded default contracts and explicit compatible overrides.

## Documentation and limits

Start with the [documentation index](docs/README.md), [architecture](docs/architecture.md), [configuration](docs/configuration.md) and [contract source policy](docs/contracts.md). The [name and path migration](docs/migration.md) explains old names in historical evidence.

Local passing tests do not establish HPC qualification, approved performance budgets or permission for a public release. License selection remains open. M1 recording is complete locally; M1.5 is preparing engineering and trial delivery, M2 will add offline analysis, M3 integration, and M4 business reports and delivery qualification.
