# SLD-04 file Registry foundation

`Registry.Register` accepts a completed RunContext or AttemptContext, the registry namespace, the parent identity and a caller-owned request token. It verifies the public context schema, context digest, token digest and deterministic ID before touching the registry path. SLD-05 will construct contexts, validate their full parent/scope semantics and expose the `sledra registry run-create` / `attempt-create` commands. This package does not submit work or create an identity when an event is emitted.

Authority path: `SLEDRA_ROOT/registry/<sha256(registry_id)>/<run|attempt>/<first two ID hex digits>/<id>.json`. The path is derived only from a full ID and a registry namespace; cycle is not in the Run key. The root must already exist as a real absolute directory on a filesystem qualified for hardlink create-if-absent and directory fsync.

Each authority file is canonical UTF-8 JSON with a closed internal field set: schema/object kind, registry/parent/identity, token and normalized registration-request SHA256 hashes, complete public context and its SHA256, creation time/role, complete `run.registered` or `attempt.registered` outbox event, and a full record SHA256. The raw request token is never stored. The stable `evt_` event ID is derived from the registration identity and event type; a duplicate returns the **winner's exact record**, including its original timestamp and event envelope. There is no separate outbox write to lose.

The publication sequence is: create/fsync the directory chain; create a mode-0600 temporary file in the target directory; write and fsync the complete record; hardlink it to the authority path without replacement; fsync that directory; remove the temporary name and fsync again. Losers also fsync the directory before reading the winner. A changed request for the same key returns `conflict`; malformed/changed authority returns `corrupt_record` and is never overwritten. Failures before publication return `registry_unavailable`; uncertain link or post-link sync/acknowledgment failures return `indeterminate`. Both are observational errors; a caller must retry lookup with the **same token**, never re-submit business work. The separate `CleanupStaleTemps` method removes old orphan names, including a published record's old temporary hardlink, without deleting an authority file.

Run on the qualified local WSL/ext4 development volume:

```bash
cd recorder
GOPROXY=off GOSUMDB=off go test ./internal/registry -count=1
GOPROXY=off GOSUMDB=off go test -race ./internal/registry -count=1
GOPROXY=off GOSUMDB=off go vet ./...
```

Tests use only temporary directories and the checked-in SLD-02 synthetic fixture. They cover 100 same-token and 100 distinct-token concurrent registrations for **both** Run and Attempt, independent processes, conflict/corruption, deterministic golden IDs, every publication fault point, abrupt process exit and stale temporary cleanup. These results qualify this package for the tested local filesystem only. Cross-node locks/visibility, power loss, shared/HPC mounts, production context construction and recorder overhead need later evidence.
