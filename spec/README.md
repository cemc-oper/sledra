# Sledra contract 1.0.0 draft (SLD-02)

This is a development baseline for the public wire format, not a released contract or recorder. The JSON files in [`1.0.0/`](1.0.0/) are the portable resources; [`build_contract.py`](build_contract.py) regenerates them. Each schema uses [JSON Schema Draft 2020-12](https://json-schema.org/draft/2020-12/), has no remote `$ref`, and is loaded from this checked-in directory. The schema URI identifies the dialect; it is not fetched at validation time. The `event-types.json` catalog covers all 24 event types listed in [03 Public information contracts](../../../docs/observability-plan/03-information-contracts.md) and contains the known event payload schemas, association levels, source kinds and producer roles. [`capabilities.json`](1.0.0/capabilities.json) declares `read_contracts`, `write_contracts` and `known_event_types` separately from software versions.

## Documents and semantic checks

| Document | Structural schema | Checks beyond JSON Schema |
|---|---|---|
| DefinitionManifest | `definition_manifest.schema.json` | `definition_id = def_ + SHA256(content excluding definition_id/content_sha256)`; unique task keys; stage/dependency references; parameter default type/enum |
| RunContext | `run_context.schema.json` | definition/expectation references; expanded task parameters and unique scope entries; context SHA256; cycle belongs to selected expectation |
| AttemptContext | `attempt_context.schema.json` | parent Run digest, declared task/parameters in scope, recomputed task instance ID, retry not self, context SHA256 |
| JobRef | `job_ref.schema.json` | platform/scheduler/cluster/job ID/submission generation compose the ID; Run/Attempt and platform parent match; reused scheduler number stays distinct |
| ArtifactVersion | `artifact.schema.json` | cycle/producer parent identity; actual version ID derived from content (path alone is insufficient); Attempt requires Run |
| ExpectedCycle/ProductExpectation | `expectation.schema.json` | plan ID derived from content, unique cycle/slot IDs, cancelled cycle reason, Run system/purpose/deployment and cycle match |
| Event | `event.schema.json` + `event-types.json` | role/source authority, association, conditional exit fields, time basis, required evidence, finish set digest and registration context binding; same ID with different content conflicts |

The checked-in helpers validate a **bundle** with one of each document and a list of events. That gives the tests enough parent context to check identities and authority. It is a contract test interface, not a producer or ingest service. A future standalone validator may load immutable parent documents from `SLEDRA_ROOT` using their references. Missing parents must fail validation; it must not search for the latest Run.

Schema objects are closed with `additionalProperties: false` unless a field explicitly says otherwise. Unrecognized data belongs in the namespaced `extensions` object (`vendor.feature` keys), where each value is an object. Known enum values are closed. Unknown `event_type` values with a valid 1.x envelope are preserved as opaque records, with association and extension data intact; they do not contribute success semantics. `task.success` is reserved and rejected because no one producer can attest to process, engine, product and delivery success together.

Time strings must be RFC3339 with an explicit `Z` or numeric offset. `occurred_at: null` requires `time_quality.reason`; `recorded_at` is never a substitute for the occurrence time. Direct process/stage boundaries require a known occurrence time. `duration_ns` and `lead_seconds` are signed-64-safe integers with explicit units; here they are nonnegative where appropriate. Source `seq` is session-local. `event_id` is UUIDv4 for ordinary facts or `evt_` plus a full SHA256 digest for stable source/registration facts; retransmission preserves the original ID and complete envelope.

Evidence references carry an ID, controlled locator, source and availability (`present`/`expired`/`unknown`), with optional digest, byte range, storage generation and retention time. A locator is not proof that a referenced file is still available. `byte_end` must be no earlier than `byte_start`. Submission events must bind both the JobRef ID and submission generation. Artifact validation must bind the exact artifact version. Delivery confirmations must bind the expectation slot, artifact version, artifact ID, recipient and rule version to the selected parent documents. A reused scheduler job number needs a distinct `submission_instance`.

## Canonical identity bytes

1. Parameter names use ASCII snake_case. Parameters are declared in the DefinitionManifest. Missing defaults are expanded before storing Run scope or AttemptContext; an undeclared, missing required, wrong-type or null parameter is rejected. All declared values participate in identity.
2. Strings and keys normalize to Unicode NFC. JSON objects sort keys lexicographically, omit insignificant whitespace, emit UTF-8 without ASCII escaping, and escape JSON control characters and U+2028/U+2029. Escaped surrogate pairs decode to one Unicode scalar; unpaired surrogates are invalid. Values are exact signed 64-bit integers, strings, booleans or null where schemas allow them; floats and NaN are forbidden, and JSON `-0` canonicalizes to `0`. `"01"` remains a string distinct from integer `1`. The wire format already uses `lead_seconds`: an upstream `0h` and `0s` both convert to integer `0` before validation, while raw unit strings are not valid `lead_seconds` values.
3. `task_instance_id` is `ti_ + SHA256(length_pack(run_id, task_key, canonical_parameters_as_UTF8))`. `length_pack` concatenates, for each NFC UTF-8 part, its **8-byte unsigned big-endian byte length** then its bytes. No separator or NUL is added.
4. A Run registry ID is `run_ + SHA256(length_pack(registry_id, "run", deployment_id, launch_token))`. An Attempt registry ID is `att_ + SHA256(length_pack(registry_id, "attempt", task_instance_id, execution_token))`. A JobRef ID is `job_ + SHA256(length_pack(platform_id, scheduler, cluster_id, scheduler_job_id, submission_instance))`. The raw tokens are caller-owned and are not recoverable from the public digest. The same token with changed request content is a conflict, not an occasion to issue another ID. Request SHA256 hashes the canonical normalized request object.
5. A document's `content_sha256`/`context_sha256` omits only the digest field itself (DefinitionManifest also omits `definition_id`). Artifact and expectation IDs similarly omit only their own ID field. Event canonical hash covers the complete event; raw ingest bytes and byte offsets are separate facts.

[`golden-vectors.json`](fixtures/1.0.0/golden-vectors.json) gives exact canonical bytes and full IDs/hashes. Fixture inputs are synthetic; [`SHA256SUMS`](fixtures/1.0.0/SHA256SUMS) pins every fixture file. This deliberately avoids GPL historical test strings and real operational logs.

## Compatibility matrix

| Writer → reader | Handling |
|---|---|
| 1.0 → 1.0 | Validate known events and semantics; duplicate identical ID/envelope is a retransmission |
| Later 1.x → 1.0 | Stable envelope and namespaced extensions remain readable; unknown event types are retained opaque, reported as unsupported semantics, never counted as success |
| 2.x → 1.x | `unsupported_major`; quarantine with original bytes in a future ingest implementation; do not apply 1.x semantics |
| Legacy ecFlow log → 1.x | SLD-16 adapter only after source/permission and timezone evidence; observed/inferred provenance retained |

Adding an optional field is minor only in an already open extension location. A new core enum, required field, unit, identity algorithm or changed meaning needs a new major version or event type. Recorders keep a fixed write version until the reader and replay fixtures are upgraded. The positive future-extension fixture checks that both 1.0 helpers preserve its unknown event object, including extensions.

## CLI and errors

The SLD-05 Run/Attempt/finish/context commands and SLD-07 `sledra event` command are implemented by [`cmd/sledra`](../recorder/context/README.md). The existing bundle helper is `python3 spec/check.py BUNDLE.json` or `cd recorder && go run ./cmd/sledra-spec-check [-spec-dir DIR] BUNDLE.json`; it returns JSON status on stdout for success and JSON `{ok:false,code,detail}` on stderr for failure. SLD-06 additionally provides a standalone offline [`sledra-event` encoder/validator](../recorder/event/README.md), which does not record to a spool and keeps stdout empty.

| Recorder command | Required input | Output / behavior |
|---|---|---|
| `sledra registry run-create --request-file FILE --context-out FILE` | JSON request with explicit launch token, registry/deployment/system/cycle, `definition_file`, optional `expectation_file` and scope; `SLEDRA_ROOT` | Empty stdout; atomic immutable Run context at the requested path; identical request reuses winner |
| `sledra registry attempt-create --context RUN.json --request-file FILE --context-out FILE` | Parent Run, task key/full parameters, execution token, optional `retry_of` | Empty stdout; immutable Attempt context; distinct execution token creates a distinct Attempt |
| `sledra context validate --file FILE` | Existing context plus immutable parent references under `SLEDRA_ROOT` | Empty stdout; nonzero on invalid/unavailable parent; never emits executable Shell |
| `sledra event --context ATTEMPT.json --type TYPE --payload-file FILE [--event-id ID] [--strict]` | Existing context, typed JSON payload; producer role from installed configuration, not arbitrary event flag | Empty stdout; business mode records bounded diagnostic and returns 0 on observation failure; `--strict` returns classified error; same-ID retry needs preserved full envelope, as explained in the [spool guide](../recorder/spool/README.md) |
| `sledra run finish --context RUN.json --scope-file FILE` | Explicit immutable covered Attempt IDs, scope/rule version/reason | Empty stdout; appends a finish declaration; later Attempt remains possible |

Recorder failures must be machine-classified on stderr without exposing secrets. Contract helper codes are `duplicate_key`, `schema_invalid`, `semantic_invalid`, `missing_parameter`, `unknown_parameter`, `parent_mismatch`, `unauthorized_event`, `conflicting_event_id`, `unsupported_major`. The SLD-04 Go Registry package classifies `conflict`, `corrupt_record`, `registry_unavailable` and `indeterminate`. The CLI emits classified JSON on stderr and empty stdout; missing or invalid contexts are never replaced by a guessed Run. SLD-07 event recording adds `spool_unavailable`, `spool_full`, `writer_busy` and business/strict failure modes; see the [spool guide](../recorder/spool/README.md). `indeterminate` means retry lookup with the **same** request token, never resubmit the business action. The bundle helper exits 0 on valid bundle and 1 on invalid bundle; its status protocol is independent of the recorder CLI.

The SLD-06 offline event command returns only a classified code on stderr for failure and empty stdout in both modes. Its additional codes include `limit_exceeded`, `sensitive_field`, `config_invalid`, `config_unavailable`, `input_unavailable`, `output_unavailable`, and `indeterminate`. The `encode` output file is an intermediate artifact, not a recording or ingestion acknowledgment. Event parent-reference checks still require the bundle helper or a later context-aware validator.

## Development entry and evidence

The tested local toolchain is WSL2 x86_64, Go `1.27.0`, Python `3.12.3`, Bash `5.2.21`, `uv` `0.11.2`, `jsonschema` `4.10.3`. The recorder helper module is `sledra.local/recorder` with pinned Go modules in [`go.mod`](../recorder/go.mod) and [`go.sum`](../recorder/go.sum); Python's direct test dependency is in [`requirements-test.txt`](requirements-test.txt). No Python runtime dependency is added to the future recorder. Remote module naming, distribution license, deployment platform and static binary qualification remain later decisions.

From the Sledra root, the repeatable checks are:

```bash
python3 spec/build_contract.py
python3 spec/build_fixtures.py
python3 spec/verify.py
cd recorder && GOPROXY=off GOSUMDB=off go test ./...
cd recorder && GOPROXY=off GOSUMDB=off go build -o /tmp/sledra-spec-check ./cmd/sledra-spec-check
```

The build helpers regenerate checked-in JSON; reviewers can compare bytes before and after. `verify.py` builds the Go helper with network module lookup disabled, runs every checked-in synthetic case through both implementations, compares classifications and complete successful outputs, verifies source SHA256 and golden identities. There is no scheduler submission, production workflow execution, recorder event write, Python ingest or HPC platform claim in SLD-02.
