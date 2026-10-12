# SLD-09 retention fixture and operational boundary

`sledra-retention` is deliberately fixture-only. It accepts a private (0700), initially empty root after `init-fixture` creates `.sledra-retention-fixture`. The marker must be created **before** any spool data. No production confirmation source or automatic cleanup is enabled. SLD-13 must supply acknowledgements from a committed raw-record/checkpoint transaction, with a durable raw location and watermark, before production cleanup can be qualified.

The fixture `ack-fixture` operation accepts only a complete, singly linked `.sealed.jsonl` beneath the root's hashed `spool/` tree. It checks the header UUID/source/session against the path and hashes all bytes. It copies the raw segment to `fixture-raw/<source-hash>/<uuid>.jsonl` via a same-directory temporary file and no-replace publication, verifies that copy, then publishes an immutable `acknowledgements/<source-hash>/<uuid>.json`. The receipt binds UUID, source, whole-file SHA-256, sealed size, raw archive location and a committed offset equal to the sealed size. This is **synthetic** persistence evidence, not an ingest commit or SLD-13 watermark. Identical repeated acknowledgements are idempotent; divergent receipts conflict.

`clean-fixture` reopens and rehashes the sealed segment, compares the receipt and archived copy, and requires both sealed and acknowledgment ages to exceed `source-days`. The policy requires positive `backlog-days`, `source-days > backlog-days`, and `evidence-days >= source-days`; expired acknowledgment evidence blocks cleanup. It writes a durable cleanup intent, rechecks the sealed inode/content, unlinks only that sealed path, syncs its directory, and writes a completion receipt. A crash after unlink but before completion leaves the intent and archive as recoverable evidence; treat that state as **incomplete cleanup**, inspect the ack/archive and restore the segment from the archive if needed before rerunning ingestion. Do not infer ingestion completion from a fixture receipt.

Example in a disposable local directory:

```sh
root=$(mktemp -d)
sledra-retention init-fixture --root "$root"
# Generate sealed segments under this root with the recorder fixture.
sledra-retention ack-fixture --root "$root" --segment "$sealed" \
  --backlog-days 1 --source-days 3 --evidence-days 30
# clean-fixture accepts the same flags after both retention clocks elapse.
```

For a real deployment, set `max_spool_bytes` and `max_spool_files` together in every producer's JSON config, using A6-approved values. The recorder checks the shared `spool/` tree under `.capacity.lock` before admitting a new writer and before each append, counting regular-file bytes and files including session locks. Reaching either bound returns `spool_full`; capacity-lock contention returns `writer_busy` immediately. Business mode reports a dropped record and continues its workload. No capacity path deletes an unacknowledged segment. These checks use `flock` and full-tree scans, so their correctness and latency on the target shared filesystem remain unverified. A6 must also approve backlog age, source/evidence retention windows, and backup recovery point. If absent, production cleanup stays disabled.

Recovery on the fixture: compare the UUID/hash/size in the acknowledgment to the archived JSONL, inspect `cleanup/*.intent.json` and `*.complete.json`, and replay the archived bytes into a fresh ingestion test. If an intent exists without completion and the sealed source is absent, the archive remains the reconstruction source. If acknowledgment or archive is missing/mismatched, preserve the sealed source and diagnose; never free space by deleting it. For real SLD-13 recovery, restore raw data and checkpoint from the **same** backup waterline before accepting any cleanup receipt. Target shared filesystem, cross-node locking, backup and real database recovery are outside this local fixture's evidence.
