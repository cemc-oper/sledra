# Sledra command line

`sledra` registers identities and records facts; the owner launches business work. The binary embeds the default 1.0.0 draft contract and runs from any working directory without the source tree. Runtime data belongs under an existing absolute `SLEDRA_ROOT`, separate from installation resources.

## Help and version

These commands need no environment configuration, producer configuration or external contract files:

```bash
build/bin/sledra --help
build/bin/sledra registry --help
build/bin/sledra registry run-create --help
build/bin/sledra registry attempt-create --help
build/bin/sledra context validate --help
build/bin/sledra run finish --help
build/bin/sledra event --help
build/bin/sledra --version
```

`-h` is an alias for `--help`; `sledra help registry run-create` is another command-help form. Invoke help on its own after the command name. Help writes human-readable text to stdout. `--version` writes one JSON object containing `software_version`, `commit`, `build_time`, `source_sha256`, `go_version`, `goos`, `goarch` and `contract_version`.

`make build` defaults to software version `dev`, embeds the current full Git commit with `-dirty` for a changed working tree, hashes the recorder input inventory, and records a UTC build timestamp. `SLEDRA_VERSION` can set a build label; it does not establish a release. `SOURCE_DATE_EPOCH` selects a reproducible timestamp. Matching builds retain their timestamp when reused. An independent source archive reports commit `unknown`; plain `go build` reports software `dev` and unavailable commit/time/source hash as `unknown`, while still reporting the actual Go toolchain and target. Software labels, contract 1.0.0 and milestone IDs are separate identities.

## Recording commands

| Command | Required flags | Result |
|---|---|---|
| `registry run-create` | `--request-file FILE --context-out FILE` | Register a Run and publish its immutable context |
| `registry attempt-create` | `--context RUN --request-file FILE --context-out FILE` | Register an Attempt under an explicit parent Run |
| `context validate` | `--file FILE` | Validate context, hashes and immutable parents |
| `run finish` | `--context RUN --scope-file FILE` | Append an explicit covered-Attempt finish declaration |
| `event` | `--context ATTEMPT --type TYPE` and payload or typed process flags | Record a context-bound event |

All recording commands require `SLEDRA_ROOT`. Events also require an installed `SLEDRA_CONFIG`; see [configuration](configuration.md). Request fields are described by the [context guide](../recorder/context/README.md). The event help lists `--payload-file`, typed process flags, optional `--event-id` and `--strict`; producer role comes from installed configuration.

Successful recording commands leave stdout and stderr empty. Lifecycle failures return 1 and JSON `{ "ok": false, "code": "..." }` on stderr. Business-mode event failures return 0, leave stdout empty and emit bounded JSON including `dropped: 1`; strict failures return 1 with the classified code. Bash hooks keep their fixed one-time diagnostic and return 0 to the owner. Help/version are explicit informational exceptions to empty stdout. There is no `sledra exec` command.

## Contracts and paths

Unset or empty `SLEDRA_SPEC_DIR` selects embedded resources: no cwd lookup, extraction or installation write occurs. To override, supply an absolute directory containing all nine canonical JSON resources, including `capabilities.json`. The override must describe exactly the installed contract. Whitespace and object-key order may differ; schema rules, catalog roles/types and capability values must match. Additional unrelated files are ignored. A later 1.x event envelope remains readable under the existing compatibility rules; replacing the installed schema with a different 1.x schema is a separate operation and is rejected.

| Override problem | Code |
|---|---|
| Nonempty relative directory | `contract_path_invalid` |
| Missing or unreadable required resource | `contract_unavailable` |
| Malformed JSON, different version, schema, catalog or capabilities | `contract_incompatible` |

Invalid overrides fail explicitly without falling back to embedded defaults. Event commands apply their existing business/strict policy to these codes. These resource errors do not echo file contents or paths. Other missing root/configuration errors retain their precedence. The developer `sledra-spec-check` helper also uses embedded defaults; its explicit `--spec-dir` flag retains support for relative test paths and reports the override code with diagnostic detail.

Use absolute request/input/output paths for direct calls from another cwd. Paths such as `definition_file` inside requests are caller-owned inputs, not installed resources, and relative input paths still resolve against the invocation directory. The [standalone owner](../examples/standalone/README.md) locates its Shell library, fixture inputs and payload relative to its script, runs in that example directory, and writes only to its explicit work directory. It uses the binary's embedded contract by default and works with a read-only installation tree.

## Development checks

`make check` regenerates canonical resources in a private copy and compares both Go embed directories byte for byte. For an intentional contract update, regenerate `spec/build_contract.py`, then run `python3 scripts/sync_contracts.py` to copy canonical resources into the embeds; review the changes and run the full regression. Never author an embedded copy independently.

`make test` includes [portable CLI checks](../tests/cli/verify.py): no recording environment, paths containing spaces, a read-only copy without source contracts, different working directories, invalid overrides and business-output equivalence. Existing contract vectors and the frozen M1 handoff remain compatibility inputs. This verifies local binary portability; trial packaging and target HPC qualification are separate work.
