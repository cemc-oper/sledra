# Sledra development workflow

Run the public Make targets from the repository root. Python configures development and verification tools only; `package = false` means the root project installs no Sledra library, and metadata version `0.0.0` is not a recorder release. The Go recorder and Bash hooks have no Python runtime dependency. An analysis package belongs to M2.

## Toolchains

Install these tools before setup. Minimum versions are the supported development floor; the tested versions below describe local evidence, not testing of every permitted version.

| Tool | Minimum | Tested locally |
|---|---|---|
| Python | 3.12 | 3.12.3 |
| uv | 0.11.2 | 0.11.2 |
| Go | 1.25.0, as declared by go.mod | 1.27.0 linux/amd64 |
| Bash | 5.2 | 5.2.21 |
| GNU Make | 4.0 | 4.3 |
| Platform | Linux with the required filesystem primitives | WSL2 x86_64, ext4 |

The platform probe uses `findmnt` from util-linux; Shell assertions use `grep`. Actual shared/HPC qualification remains separate. You can select the bootstrap interpreter with `make PYTHON=/path/to/python3 ...`, the uv executable with `UV=/path/to/uv`, and the project interpreter with `UV_PYTHON=/path/to/python3`.

## Online preparation

```bash
make help
make setup
```

`make setup` is the explicit network step. It synchronizes the committed [uv.lock](../uv.lock) using [pyproject.toml](../pyproject.toml), then downloads the Go modules pinned by [go.mod](../recorder/go.mod) and [go.sum](../recorder/go.sum). It uses the installed Python and Go toolchains and disables automatic Python/Go toolchain downloads. Dependency caches may start empty. Site proxy settings still apply to this online step.

The developer group pins jsonschema 4.10.3 and Ruff 0.16.9, with attrs and pyrsistent locked transitively. [requirements-test.txt](../spec/requirements-test.txt) remains the narrow compatibility input for direct contract helpers; `make check` confirms its jsonschema pin agrees with the root configuration. Update the root metadata and lock deliberately when changing dependencies. Ordinary commands require an up-to-date lock and do not rewrite it.

## Offline daily commands

```bash
make build
make check
make test
make demo
make benchmark
```

These commands disable uv network access, Go proxy/sum database lookup, private-module proxy bypass and toolchain downloads. They need the dependency caches prepared by setup; offline source builds with empty caches are unsupported. An existing synchronized Python environment is used through uv. An offline environment may also synchronize from the prepared uv cache if its project environment is absent.

`make build` creates five tools in `build/bin/`: sledra, sledra-spec-check, sledra-event, sledra-spool and sledra-retention. `build/manifest.json` records Go inputs, target/toolchain, build flags and binary SHA-256. Builds use `CGO_ENABLED=0`, `-trimpath` and `-buildvcs=false`, so an archive without Git metadata can build. Matching outputs are reused; changed inputs or changed executable bytes trigger rebuilding. Build provenance is embedded in `sledra --version`; see [CLI metadata](cli.md#help-and-version). This developer manifest is not a release-package manifest.

`make check` checks lock freshness, Python formatting of maintained scripts, Python import/syntax/unused-name lint across the project, Go formatting and vet, Bash syntax, generated contract bytes and local documentation links. Legacy contract/test formatting remains unchanged. Contract and fixture generation runs in a private temporary copy, compares file inventories and bytes, and checks both embedded event and context resources; it never regenerates the source tree during an ordinary check.

`make test` builds or reuses the tools once, then runs uncached Go tests, engineering regressions, portable CLI and existing contract/event/context/Shell assertions, the M1 scenario and the frozen replay. The Python helpers receive `SLEDRA_TEST_BIN_DIR` and reuse those tools. An explicit missing binary fails rather than silently rebuilding. Without that developer override, direct script invocation retains its temporary offline build behavior.

`make demo` creates a fresh report directory and runs the normal/failure/retry synthetic M1 scenario, exports a new handoff there, and replays it. Its snapshot includes the raw contexts, Registry, finish and JSONL content for inspection. `make benchmark` runs the separate existing timing probe, retaining raw nanosecond samples and statistics in that run's `benchmark.json`; set `SLEDRA_BENCHMARK_SAMPLES=10` to shorten a local run (allowed range 10–100). No approved performance qualification follows from this measurement.

## Artifacts and failures

`build/`, `dist/` and `test-results/` are ignored by Git. Build outputs live under `build/`; `dist/` holds local trial archives, manifests and checksums. Check/test/demo/benchmark/package/test-package each create a unique `test-results/<action>-<timestamp>-<id>/` containing stage logs and `summary.json`. Failing stages stop the command, return nonzero, print the stage and log path, and leave a failure summary. The report also compares protected fixture inventories and bytes before/after the command.

Do not overwrite the frozen M1 or historical platform/timing reports during daily work. Deliberate refresh remains the separate procedure in the [M1 guide](../tests/m1/README.md). The [contract source review](contracts.md) remains optional authoring-workspace validation, independent of standalone developer setup.

## Source and package modes

Source development requires the tools above and prepared Python/Go dependencies. The [precompiled local trial](installation.md) requires the target binary and Bash for recording; its synthetic example also uses dirname/cat/sleep. Standard-library Python verification is separate. `make package` and `make test-package` deliver and accept the local trial, without claiming full offline release/upgrade qualification. The recorder embeds its default contract; [CLI help/version](cli.md) and [configuration](configuration.md) describe portable calls and explicit runtime paths.

`make test-platform` runs the single-node primitives and local cross-node simulation. Individual source/installed scripts share [binary, spec, work, report and retention parameters](testing-hpc.md); compute-node mode requires an explicit work parent.
