# CE-1 child-events contract and independent vectors

CE-01 contract baseline, 2026-10-30. This directory defines the opt-in interface
`CE-1` and node-local control protocol `execution-child-events/1`. The software
remains `dev`, Execution JSONL remains format `2.0.0` and layout `2`. These are
separate version axes. No new software release or data migration is implied.

**CE-02 now implements the Linux Go coordinator and internal CLI; the public Bash API remains CE-03 work.** See the [component implementation](../../recorder/execution/child-events.md). The checks here
are offline contract oracles, synthetic records and compatibility expectations.
They do not prove process identity, filesystem atomicity, lock behavior, actual
Bash no-op behavior, mixed-install runtime rejection or GFS/HPC execution.

## Normative sources

- [Shell/CLI interface](interfaces.md): P01–P02, opt-in/public hooks, exact handshake,
  prepared/receipt grammar, typed arguments, distinct CE exit codes.
- [Runtime protocol](runtime.md): P03–P09, descriptor/state/closing fields,
  identity and permission requirements, state priority, admission and stop
  linearization, receipt-loss rules, diagnosis and cleanup boundaries.
- [Control schema](contract.schema.json): closed JSON objects for descriptor,
  state, pending, last result and closing; only local `$ref` targets. This schema
  does not replace the unchanged [Execution 2.0.0 schema](../execution-v2/contract.schema.json).
- [Version identities](versions.json), [byte budgets](limits.json) and
  [D01–D12 source mapping](source-map.json) are machine-readable inputs. The source
  map retains the CE-00 text digest and points to the original repository clauses.

The interface/runtime text incorporates the CE-00 P01–P09 decisions. The following
wire details make their validation unambiguous without changing the protocol:

- Control JSON numbers are integer tokens; versions/UID/PID/revision do not accept
  boolean or fractional tokens. Device/inode/start ticks remain canonical decimal
  strings within uint64, with the nonzero constraints specified in P03.
- A descriptor digest covers its exact bytes, including optional trailing LF or
  whitespace. Owner-key SHA256 covers the decoded 32 random bytes, not their hex
  text. A prepared digest covers event JSON bytes excluding the LF delimiter.
- `prepared` and completion receipts each have exactly nine nonempty TAB-separated
  fields and one terminal LF. Capability success is the separate exact three-field
  line `CE-1<TAB>execution-child-events<TAB>1<LF>`.
- `committed`/`noop`/`confirmed` require both IDs and the original known revision.
  `attached` success belongs to `attach` with phase active; `confirmed` belongs to
  `result` and cannot restore a disabled session. An open success must carry a valid
  REF; other completions carry `-` in auxiliary. `disable` reports code 23 with its
  actual local/requested/quiescent scope; there is no invented success status.
- argv payload counts the UTF-8 bytes of each supplied argument plus its NUL
  separator. This is a protocol budget, not a claim about a platform's ARG_MAX.
  JSON strings cannot smuggle NUL into an actual argv.
- Offline `ContractError` codes include unchanged 2.0.0 validation codes such as
  `unsupported_version`. They are not CLI exit codes or permission to change the
  old internal `prepare/append` meanings (0/2/3). CE CLI codes are 0 and 20–26.

## Independent vectors

All identities, paths, process metadata and business records here are synthetic.
The case files are inputs with explicit expected outcomes, not snapshots emitted
by a CE runtime. The runtime must consume these vectors in CE-02–04; it must not
import these Python reference checks or add a Python dependency to recording.

| Input | Coverage | Acceptance scope |
|---|---|---|
| [Control/wire vectors](cases/vectors.json) | Descriptor/state/closing acceptance and rejection, duplicate/unknown fields, versions, numeric/identity relations, caches, frame corruption, exact byte limits and old/new exit codes | A01/A05 contract |
| [Role vectors](cases/roles.json) | Event-only producers, owner lifecycle, owner-only creation and reserved event prefixes | A05 contract |
| [Admission decisions](cases/admission.json) | off/unattached no-op, healthy Busy, abandoned writing, closing/disabled priority, context/end idempotence/conflicts, explicit end after Busy and stage order | A05 plus future A06–A09 requirements |
| [Recovery decisions](cases/recovery.json) | Matching last result, overwritten/missing slot, busy/unreadable control, lost result response, history confirmation without re-enabling | Future A09 requirement |
| [Diagnostic decisions](cases/diagnostics.json) | Silent no-op/Busy/closed, execution versus process budget, fixed diagnostic trigger categories | Future A13 requirement |
| [Compatibility combinations](cases/compatibility.json) | Old library/old binary, old/new and new/old defaults, opt-in mismatch, exact new/new protocol, off and ambiguous/unknown modes | A01 contract, runtime evidence later |
| [Typed values](cases/typed-values.json) | Exact int64 and decimal values, quoted literal text/Unicode, null/empty, negative argv values, duplicates, invalid numbers, string limits | A05 contract |
| [Synthetic JSONL](cases/child-events.jsonl) | One started/context/ended and 0/3/6/9/12 ready events; full LF lines, exact integer >2**53, Unicode and typed attributes | A01/A14 data compatibility |

Vector recipes in `vectors.json` start from `base`, apply `set` to slash-separated
object keys and `remove` to top-level keys, then optional wire `fields` by zero-based
index. `prefix` inserts bytes after the initial `{` (duplicate-key cases), followed
by `suffix`/`suffix_hex`, optional space padding to `pad_to` bytes and a named frame
`transform`. Expected rejection codes are explicit. These bounded recipes avoid
checking megabytes of repeated padding into Git; they are deterministic inputs,
not outputs derived from the validator.

Byte-budget checks cover every limit at N and N+1. For control JSON, full-size
padded valid objects additionally test actual parsing at the limit. A valid event
is accepted at 65536 bytes by the original checker and rejected at 65537. UTF-8
paths/argv test byte counts rather than character counts. The prepared, receipt
and REF budgets contain deliberate framing headroom: a byte budget acceptance at
N does not assert that arbitrary N-byte content is a valid frame. Grammar,
embedded-event and path limits are tested separately; no old event limit expands.

`ce_contract.py` checks static records, wire framing, roles, bindings and budgets.
`semantics.py` evaluates abstract decisions: `append_max` is a permitted upper
bound, never an observed Write count. Recovery inputs assume the slot has already
passed control-record validation. Diagnosis inputs describe budget availability,
not an actual O_EXCL competition. Their passing results are not runtime evidence.

## Version compatibility expectations

| Combination | Default Execution path | Opt-in CE path |
|---|---|---|
| Old library / old binary | Preserve existing behavior | Unsupported; no inferred parent context |
| Old library / new binary | Preserve old protocol and 0/2/3 | Old library cannot enable CE |
| New library / old binary | Preserve old protocol without CE handshake/runtime directory | Reject handshake, no legacy append fallback |
| New library / new matching binary | Preserve old protocol by default | All owner/producer calls use protocol 1 |
| New pair, different interface/protocol | Default remains independent | Reject before creating/publishing context |
| New event library without a parent REF | No implicit begin | Silent no-op, no files/CLI/diagnosis |

The Go capability command is delivered in CE-02; the public Bash libraries remain CE-03 work.
A shared software version string or JSON format is not evidence of CE support.
Handwritten old append/OpenWriter calls are outside CE support; unchanged JSONL
cannot force an old binary to honor new control files. The P08 limit remains
explicit, as do double stop-publication failure and owner replacement with exec.

## Public verification entry

From the Sledra repository:

```bash
uv run --locked --offline --no-python-downloads python spec/execution-child-events/verify.py
make check
make test
```

Both root `make check` and `make test` invoke this gate; the old
`spec/execution-v2/verify.py` still runs independently. No baseline regeneration
occurs during verification. [frozen-inputs.json](frozen-inputs.json) records 140
existing specification/fixture/embed files, including the complete Execution v2
specification and the prior protected directories; tests fail on drift rather
than rewriting originals. Future intentional data-contract changes need separate
review, not a routine manifest refresh.

The Python gate validates the synthetic JSONL with the unchanged original checker
and `sledra_analysis.execution.reader.FileReader`, checks exact values and proves
no files changed during reading. Node-local controls are outside the record root.
The Go test `TestChildEventsContractUsesUnchangedExecutionCodec` in
[the execution package](../../recorder/execution/child_events_contract_test.go)
passes the same eight records through the unmodified Go codec and asserts ready
leads and exact integer precision. It runs with normal `go test`/`make test`.
These are data interoperability checks, not real child-process writing.
