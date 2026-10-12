# Sledra recording architecture

Sledra is one product in one repository. Go implements recording, Bash integrates explicit hooks into known owners, and portable JSON contracts define identities and events. Python currently supplies test helpers; the offline analysis package belongs to M2.

The lifecycle owner creates a Run with an explicit launch token, then creates an Attempt for a task instance with an execution token. The [Registry](../recorder/internal/registry/README.md) publishes each immutable authority record and its registration outbox event atomically. A repeated identical request reuses its identity; changed content with the same token conflicts. A new business attempt uses a new token and may identify its retry parent.

The [context service](../recorder/context/README.md) publishes immutable contexts at caller-selected absolute paths under the same runtime root. Consumers validate the precise parent records and digest references. They never infer identity by looking up the latest Run for a cycle. A [context-aware event call](../recorder/spool/README.md) checks that association, encodes canonical bytes and appends to the producer's session spool.

The [Bash API](../shell/README.md) only defines functions. The owner keeps its options, traps, callbacks and payload execution, captures the original exit code or signal, then emits lifecycle facts. Identity failure disables subsequent observation; event failure follows the business/strict policy. An observation retry must never rerun the payload.

Run finish is a separate declaration freezing an explicit covered Attempt set. A later Attempt remains possible and does not rewrite an earlier declaration. Analysis of completeness or success, durable ingestion acknowledgements, and production retention are future components. See the [contract guide](../spec/README.md) for exact identity bytes, event authority and version compatibility.

Registry, contexts, documents and spool share one `SLEDRA_ROOT`. Cross-node consumers must use the same absolute context path on a qualified shared filesystem. Current tests establish local WSL/ext4 behavior only. The source and synchronization policy is in [contract sources](contracts.md).
