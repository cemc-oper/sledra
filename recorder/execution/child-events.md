# Coordinated child events (CE-02)

CE-02 implements the Linux Go coordinator and the **internal**
`execution child-events` command family for [CE-1](../../spec/execution-child-events/README.md).
The public owner opt-in and `sledra-events.sh` Bash API remain CE-03 work.
GFS still uses the existing interface until its separately gated integration.
No legacy Registry, Attempt, spool, Python, jq, ecFlow or daemon is involved.

`ChildRecorder` reuses the Execution codec, root validation and sequential Writer.
New files isolate bounded control decoding, prepared framing, kernel identity,
private file operations and operation coordination. The old prepare/append path
is unchanged; its 0/2/3 exit protocol is not used for CE receipts. The independent
control schema is embedded and checked against its canonical specification.

Supported runtime builds are Linux amd64 and arm64. Other hosts keep the legacy
build path; the CE capability handshake rejects unsupported platforms. Runtime
permission checks reject symlinks, non-private session/control files, hardlinks,
changed lock/directory/record inodes and writable ancestors, including `/tmp`.
Use a pre-created node-local 0700 runtime under trusted ancestors (for example a
private user cache). The deployment must verify local filesystem semantics;
absolute paths alone do not establish that a mount is node-local. No runtime
base is guessed or created. Target filesystem qualification remains CE-07.

An owner is bound to UID, boot identity, PID namespace, Bash PID/start ticks and
an unexported owner key. Owner operations additionally require Bash ancestry and
that key. Producers receive only an event-only reference. Every call rechecks the
binding; missing state/descriptor/lock and stale references never rebuild state
or identity. Root configuration and record identity stay fixed at begin.

One stable nonblocking flock serializes every append for an Execution. An intent
is atomically published before touching JSONL; one complete LF Write and checked
Close precede state commit. Input rejection and Busy do not disable a healthy
session. A crashed writer leaves writing, which the next lock holder disables
without replay. The state is loaded again under lock; a complete immutable old
snapshot observed before locking is not confused with an abandoned writer.

The first legal end publishes the complete immutable closing claim before trying
the lock. It can return Busy with admission closed. A later explicit identical end
can use that first claim only when no end write was attempted. New events are
rejected after closing; the admitted writer may drain before ended. Context/end
comparison reuses exact Execution equality, including UTC nanoseconds for cycle
and exact decimal numbers. Stages remain single and sequential.

Disabled and closing are monotonic files. Publishing a stop request differs from
confirming no writer remains: receipts report local, requested or quiescent scope.
A writer never clears concurrent gates when committing state. Failed gate
publication falls back to a durable disabled state or retained writing when held;
if neither can be published, only local stop is reported. External deletion of
monotonic gates, active lock cleanup, direct old-writer calls and malicious
same-UID mutation are not supported. No claim of mandatory access enforcement or
cross-file power-loss atomicity is made.

The last-result slot is bounded. `result` takes the original prepared request and
its three identifiers, obtains the same lock once and only confirms a matching
committed slot without writing. Missing/overwritten slots, lock Busy, unreadable
state or residual writing yield unknown. The CE-03 caller must then invoke
`disable` once; lookup itself stays read-only. Historical confirmation never
re-enables disabled state, and no operation automatically repeats prepare/append.

Fault injection is private to Go tests; there are no production fault environment
variables or CLI flags. Tests cover intent/commit publication cuts, data short
writes, full writes returning error, Close errors, start publication failure,
closing/disabled races, state corruption and identity/inode replacement. Component
owner fixtures are explicitly synthetic; real Bash ancestry, process death and
fresh CLI calls are checked separately in
[the native CLI tests](../../tests/execution/test_child_runtime.py).
The public make test includes both [Go components](child_runtime_linux_test.go)
and native CLI checks; the existing CE-1 vectors remain independent.

This is component qualification. Public Bash behavior, full SIGKILL/fault/barrier
acceptance, installation combinations, GFS business equivalence and HPC operation
remain CE-03–07. There is no automatic control-directory cleanup or history repair.
