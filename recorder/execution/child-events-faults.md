# CE-04 local cross-process fault acceptance

The [Bash/Go acceptance suite](../../tests/shell/test_execution_child_faults.py) runs
independent Bash owners/producers and actual owner-spawned child shells against the
CE-1 coordinator. It supplements the [public interface regressions](../../tests/shell/test_execution_child_events.py)
and [Go component tests](child_runtime_linux_test.go). All are included in the
existing root `make test`; no production inputs or scheduler submissions are used.

The [process helper](child_process_helper_linux_test.go) is compiled only by
`go test -c`. Its private controls are absent from production binaries. Normal
encoding, capability, binding and diagnostic calls delegate to the actual CLI;
coordinated calls invoke the actual recorder with real procfs owner identity,
private files, nonblocking flock, atomic rename, JSONL Write and Close. Unix socket
ready/release barriers select interleavings; sleeps do not decide admission order.
Injected errors/short writes are test boundaries, not claims of a physical device
failure. The suite also proves production binaries ignore these test controls.

The writer hook is installed at file creation, before the initial Append, so both
started and later writes can be observed. Ordinary roots have a nil hook and retain
single-Write/checked-Close behavior. A private cut between the final disabled and
closing reads covers the admission group's linearization window. Trace rows mark
admission, intent/state publication, Write invocation/return, Close, stop receipts
and quiescence. A pre-call trace is not proof that a syscall has already entered
the kernel: the suite deliberately permits the one admitted writer to invoke
Write after a stop request, then proves zero new calls after quiescence.

Coverage includes initialization/publication failure, serial and six-producer
contention, independent executions, 14 intent/data/state fault cuts, short and
failed/full-with-error writes, Close failure, writer SIGKILL, closing at eight
boundaries, disabled at nine boundaries, one-slot result confirmation/overwrite/
Busy/unreadable or lost transport, uncertain end, control/record damage and identity
mismatch. Parent TERM/child exit status and owner death remain separate facts;
an admitted Go writer may drain even when its producer Bash is killed. End never
fills in missing stage ends or retries an uncertain ended write.

A repeated contention run exposed a state metadata race: atomic replacement can
leave either fstat or path lstat reporting a complete old inode with link count
zero. Readonly state snapshots now apply the same private-file predicate to both
metadata observations, followed by immutable session/key checks and a locked state
reload. Immutable controls/lock still require a single linked fixed inode. A real
unlinked-file regression plus the control hardlink/mode cases guard this change;
healthy contention must continue to return only success/Busy.

For durable acceptance artifacts, set `SLEDRA_CE04_EVIDENCE` to a new private output
directory while running the existing Shell unittest entry point or `make test`.
Each controlled case retains raw JSONL (including intentional short tails), state/
controls, complete event bytes without owner secrets, receipts, operation counts
and actor output. The unchanged CE-1 checker validates coordinator receipts; the
unchanged Execution validator and Python reader compare valid streams and report
partial tails without repair. These are development dependencies only.

Double failure of disabled/state publication is an explicit counterexample:
`stop=local` does not stop another healthy producer. Owner same-PID exec cannot be
revoked by PID/starttime alone; new begin generates a new ID/key, but arbitrary exec
replacement remains unsupported. Boot/namespace/start-tick mismatch cases inject
binding differences; they do not simulate actual machine reboot or force kernel PID
reuse. External active-control deletion, old/manual writer bypass, malicious same
UID mutation and power loss are outside the strong guarantee.

CE-04 qualifies the local core only. GFS application behavior, isolated installation
combinations, target filesystem/node cancellation and release qualification remain
CE-05–08. The conformance report retains `full_matrix_pass=false` and
`production_jobs_executed=0`; local test barriers are not HPC evidence.
