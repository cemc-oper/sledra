#!/usr/bin/env bash
# Controlled synthetic owner: the payload is launched exactly once by this script.
set -euo pipefail

example_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo_dir=$(cd -- "$example_dir/../.." && pwd -P)
cd -- "$example_dir"
: "${STANDALONE_WORK_DIR:?set an existing writable test directory}"
export STANDALONE_WORK_DIR
export SLEDRA_ROOT=${SLEDRA_ROOT:-$STANDALONE_WORK_DIR/observability}
export SLEDRA_CONFIG=${SLEDRA_CONFIG:-$example_dir/fixtures/process-config.json}
export SLEDRA_ENABLED=${SLEDRA_ENABLED:-0}
export STANDALONE_EXECUTION_LOG=${STANDALONE_EXECUTION_LOG:-$STANDALONE_WORK_DIR/executions.log}
export STANDALONE_CALLBACK_LOG=${STANDALONE_CALLBACK_LOG:-$STANDALONE_WORK_DIR/callbacks.log}
export STANDALONE_PRODUCT=${STANDALONE_PRODUCT:-$STANDALONE_WORK_DIR/product.txt}

# The library defines functions only. These handlers belong to the example
# owner and are identical in enabled and disabled runs.
# shellcheck source=../../shell/sledra.sh
source "$repo_dir/shell/sledra.sh"
run_context=$SLEDRA_ROOT/contexts/run.json
attempt_context=$SLEDRA_ROOT/contexts/attempt.json
SLEDRA_OWNER_SIGNAL=0
payload_pid=
on_exit() {
    local original_rc=$1
    trap - EXIT
    if [[ $SLEDRA_OWNER_SIGNAL != 1 ]]; then
        sledra_attempt_exited "$original_rc" forecast_program
        if [[ $original_rc == 0 ]]; then
            sledra_run_finish "$run_context" fixtures/finish-request.json
        fi
    fi
    printf 'exit:%s\n' "$original_rc" >> "$STANDALONE_CALLBACK_LOG" || :
    exit "$original_rc"
}
on_signal() {
    local name=$1 number=$2
    SLEDRA_OWNER_SIGNAL=1
    sledra_attempt_signaled "$number" forecast_program
    printf 'signal:%s\n' "$name" >> "$STANDALONE_CALLBACK_LOG" || :
    if [[ -n $payload_pid ]]; then
        kill -s "$name" "$payload_pid" 2>/dev/null || :
        wait "$payload_pid" 2>/dev/null || :
    fi
    trap - "$name"
    kill -s "$name" "$$"
}
trap 'on_exit "$?"' EXIT
trap 'on_signal TERM 15' TERM
trap 'on_signal INT 2' INT
trap 'on_signal HUP 1' HUP

sledra_run_begin fixtures/run-request.json "$run_context"
sledra_attempt_begin "$run_context" fixtures/attempt-request.json "$attempt_context"
if [[ -n ${STANDALONE_CONTEXT_OVERRIDE:-} ]]; then
    # Synthetic fault input for the verification harness; no context lookup
    # or fallback is attempted by the business command.
    SLEDRA_ATTEMPT_CONTEXT=$STANDALONE_CONTEXT_OVERRIDE
fi

sledra_attempt_started standalone-invocation-1 forecast_program
if [[ ${SLEDRA_STAGE_EVENTS:-1} == 1 ]]; then
    SLEDRA_CONFIG=$example_dir/fixtures/stage-config.json \
        sledra_event_file stage.started fixtures/stage-start.json
fi

# Do not place the payload inside if/|| or a command substitution: that would
# change Bash errexit behavior inside the payload. No observation fallback runs it.
./payload.sh &
payload_pid=$!
wait "$payload_pid"
payload_pid=

if [[ ${SLEDRA_STAGE_EVENTS:-1} == 1 ]]; then
    SLEDRA_CONFIG=$example_dir/fixtures/stage-config.json \
        sledra_event_file stage.ended fixtures/stage-end.json
fi
