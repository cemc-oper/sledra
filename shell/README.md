# Thin Bash API (SLD-08)

Source [`sledra.sh`](sledra.sh) from a known Bash lifecycle owner. It only defines functions: no option, trap, payload command or callback is installed or replaced. Set `SLEDRA_ENABLED=1` and absolute `SLEDRA_BIN`, `SLEDRA_ROOT`, `SLEDRA_CONFIG` paths. `SLEDRA_SPEC_DIR` is optional: unset/empty uses embedded contracts; a nonempty override must be absolute and compatible. If disabled, the functions are inert. Identity calls set `SLEDRA_RUN_ACTIVE` and `SLEDRA_ATTEMPT_ACTIVE`; on any failure they disable later observation and always return 0 to the owner. Every event call is best effort and returns 0. A missing binary or failed recorder call adds at most one fixed, payload-free diagnostic line per sourced shell to stderr, never stdout. This does not impose a hard latency bound on kernel I/O.

```bash
source /path/to/sledra.sh
sledra_run_begin /path/to/run-request.json /shared/contexts/run.json
sledra_attempt_begin /shared/contexts/run.json /path/to/attempt-request.json /shared/contexts/attempt.json
sledra_attempt_started invocation-1 forecast_program
sledra_event_file stage.started /path/to/stage-start.json
# Execute the original payload once in its original owner context.
# In an existing EXIT/signal handler, save $? or the signal first:
sledra_attempt_exited "$saved_rc" forecast_program
sledra_attempt_signaled "$saved_signal_number" forecast_program
sledra_run_finish /shared/contexts/run.json /path/to/finish-request.json
```

`sledra_attempt_started`, `sledra_attempt_exited` and `sledra_attempt_signaled` pass typed flags to Go; the Shell layer never constructs JSON. Other events use a producer-owned JSON payload file. The owner must keep its existing option, trap and callback logic, capture `$?`/`PIPESTATUS` before another command, and invoke these hooks only at known lifecycle points. No function wraps an inline command in `if`, `||` or command substitution. The API does not implement a transparent `sledra exec` supervisor. In particular, arbitrary pipeline/MPI/process-group behavior and prompt INT forwarding have not been qualified; invoking `sledra exec` is rejected. Do not rerun the payload when registration or observation fails.

The [standalone example](../examples/standalone/README.md) is a controlled Bash owner and synthetic payload. Its local test compares observation disabled/enabled under `set -e`, `set -u`, `pipefail`, existing traps, pipeline/background wait, success, nonzero exit, TERM/HUP and KILL. This is WSL/ext4 evidence, not a qualification for an ecFlow/HPC script or target shared filesystem.
