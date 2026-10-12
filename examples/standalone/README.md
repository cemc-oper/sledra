# Standalone synthetic recording example (SLD-08)

This example uses only Bash, the Go `sledra` recorder and a local test directory. It does not start an analyzer, scheduler, ecFlow or takflow, and does not run real MCV work. [`run.sh`](run.sh) owns its existing EXIT/TERM/HUP/INT handlers and callback file; it registers a Run and Attempt, records attempt/stage start, runs [`payload.sh`](payload.sh) once, records stage end and process exit, and declares Run finish on success. `payload.sh` writes a synthetic product, exercises a pipeline and background `wait`, and can return 7 or wait for a signal. The Shell library does not install handlers or run the payload.

From the Sledra repository root:

```bash
(cd recorder && GOPROXY=off GOSUMDB=off go build -o /tmp/sledra-example ./cmd/sledra)
mkdir -p /tmp/sledra-example-work/observability
STANDALONE_WORK_DIR=/tmp/sledra-example-work \
SLEDRA_ROOT=/tmp/sledra-example-work/observability \
SLEDRA_BIN=/tmp/sledra-example SLEDRA_ENABLED=1 \
  examples/standalone/run.sh
python3 tests/shell/verify.py
```

The script locates the library, inputs and payload relative to its own location and uses embedded contracts by default. It can run from another cwd with a read-only resource tree; all output belongs to the explicit work directory. An explicit `SLEDRA_SPEC_DIR` is preserved and validated by the recorder. Use a fresh test directory for each run. To compare the original behavior, run the same script with `SLEDRA_ENABLED=0`. The fixture Run/Attempt tokens and the covered Attempt ID in `fixtures/finish-request.json` are fixed for this synthetic case; changing a fixture requires updating the finish fixture. `SLEDRA_CONFIG` may override the process config for failure tests. `STANDALONE_CONTEXT_OVERRIDE` is a synthetic fault input, not a production fallback mechanism. `STANDALONE_PAYLOAD_MODE=fail` returns 7; `hang` waits for a signal. The output and callback logs are written only under `STANDALONE_WORK_DIR`.

The supported local profile is explicit owner hooks with ordinary success/failure and tested TERM/HUP re-raise behavior. SIGKILL leaves no tail event. On this non-interactive Bash background-`wait` profile, SIGINT does not interrupt promptly; the test records that limit and does not qualify it for deployment. No transparent `sledra exec` supervisor is delivered: launch failure after an uncertain start, process-group forwarding and internal `errexit` transparency require a separate proof. Target shared/HPC filesystem, scheduler and real business callback tests remain unverified.
