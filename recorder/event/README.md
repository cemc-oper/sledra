# Offline event codec (SLD-06)

`event.New(installedRole)` compiles embedded copies of the public `1.0.0` event envelope and 24-type catalog. `Codec.Encode(Envelope)` and `Codec.Validate([]byte)` use the same validation path and return canonical JSON with one trailing LF. The Go test compares both embedded resources byte-for-byte with `spec/1.0.0`. `NewID()` generates an ordinary UUIDv4; persist the emitted bytes and reuse the entire event on retransmission. Registration/source-stable `evt_` IDs come from their owner, not this generator.

The codec checks the envelope, known payload schema, installed producer role, source kind, association level, time/exit/finish rules, size and UTF-8/duplicate-key limits. It strips raw validation details from returned errors, rejects obvious secret keys, control characters and raw absolute/home/traversal/`file://` paths, and preserves unknown 1.x event types and namespaced extensions as opaque canonical JSON. Known payload schemas are closed. Producers remain responsible for redacting allowed string values and using approved locator aliases. A field denylist cannot detect every secret hidden in allowed values.

`Codec.Validate` does not load Run/Attempt/Job/Artifact/Expectation parent documents. Identity bindings that need those parents must also pass the SLD-02 bundle checker now, then the SLD-05 context lookup in the recorder. Opaque events never establish success. Major 2.x is rejected. The codec writes no event; the [SLD-07 spool](../spool/README.md) owns persistence and the business/strict failure mode.

The standalone command is an offline encoding stage, separate from the implemented `sledra event` recording command:

```bash
cd recorder
GOPROXY=off GOSUMDB=off go build -o /tmp/sledra-event ./cmd/sledra-event
/tmp/sledra-event validate -config /path/to/installed-role.json -input /path/to/event.json
/tmp/sledra-event encode -config /path/to/installed-role.json -input /path/to/event.json -output-dir /path/to/existing-dir
```

The config is JSON such as `{"producer_role":"process_owner"}` and is provisioned by the deployment, not supplied as a role on each event. `encode` atomically creates `<event_id>.json` with mode 0600 under the existing directory. Identical retries succeed; a different event with that ID returns `conflicting_event_id`. Both commands keep stdout empty. Errors are classified as a single JSON object on stderr with nonzero exit status and no input content. The output file is an offline artifact, not a spool segment or ingestion acknowledgment. The caller must choose a trusted directory; the command rejects directory symlinks in its path.

Run `python3 spec/verify_event.py` at the repository root for the Go/Python byte comparison over synthetic fixtures. Build/runtime use embedded resources and no network; parent-document and shared/HPC checks belong to later tasks.
