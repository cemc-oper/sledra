# Session spool (SLD-07)

`spool.Open` creates a source/Run/task/Attempt directory using SHA-256 path components and takes a nonblocking per-session `flock`. Each session appends canonical bytes from the embedded event codec to one `.open.jsonl` segment at a time. The first line is a versioned segment header with a UUID. Rotation syncs and seals with a same-directory no-replace hard-link publication followed by removal of the open name. A crash between those operations can leave both names for the same inode/UUID; readers must deduplicate by segment identity. Sealed segments are never reopened. An incomplete open tail is left in place and a later writer starts another segment. No unacknowledged segment is deleted.

The writer bounds each event using SLD-06 limits, each segment by `segment_bytes`, and each session by `session_bytes` and `segments`. Zero settings use **synthetic local defaults** of 1 MiB, 64 MiB, 64 segments and five minutes; these are not site budgets. `Stats` exposes successful, dropped, byte and new-segment counters for a live writer. Business mode suppresses a recording error and emits one classified, payload-free diagnostic per failed CLI call; strict mode returns a classified error. Counters are in process memory, not durable across CLI invocations. Global spool quota, acknowledgment, retention and durable diagnostics belong to SLD-09. Files are 0600 and created directories 0700; paths derived from event identities do not contain raw task keys or product paths.

The public context-aware command is `sledra event`. Set `SLEDRA_ROOT` to an existing trusted directory, `SLEDRA_SPEC_DIR` to `spec/1.0.0`, and `SLEDRA_CONFIG` to an installed JSON file with `producer_role`, `producer_name`, `producer_version`, `source_id`, `session_id`, and optional `segment_bytes`, `session_bytes`, `segments`. A truly concurrent producer must have a distinct session ID. The config is a deployment input, not a per-event role override.

```bash
sledra event --context /trusted/attempt.json --type attempt.started \
  --payload-file /trusted/payload.json --event-id UUID
sledra event --context /trusted/attempt.json --type attempt.started \
  --payload-file /trusted/payload.json --event-id UUID --strict
```

The command validates the Attempt context and its parent, encodes the complete event before append, and leaves stdout empty. It is a one-shot emission command: repeating it with the same `--event-id` generates a new timestamp, so it is not an exact-byte retry. A producer that retries must retain the complete encoded envelope and replay it through `sledra-spool append --config FILE --input EVENT.json [--strict] [--seal]`, which validates the event and installed role. That lower-level command does not verify parent context; the original envelope must come from a context-validated producer. An omitted ID is generated for that invocation only. The SLD-08 Shell example emits one-shot lifecycle facts and does not claim replay after an uncertain append; durable producer retry remains a later integration concern.

Local WSL/ext4 tests cover lock conflicts, session reuse, concurrent sources, rotation, synthetic limits, invalid/oversize input, partial writes, root symlinks and business/strict diagnostics. A read-only target and I/O failure are represented by injected failures; no actual full disk, cross-node lock or target shared/HPC filesystem was exercised. Synchronous kernel I/O may hang beyond a user-space timeout. This writer is qualified only for the tested local filesystem until SLD-03/25 qualifies the deployment target.
