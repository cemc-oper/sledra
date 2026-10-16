#!/usr/bin/env bash
# Explicit process owner. Observation calls never replace the business result.
set -uo pipefail

task=$1
job_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
if [[ -f ${SLEDRA_SHELL:-} ]]; then
    source "$SLEDRA_SHELL"
fi

observe() {
    if declare -F "$1" >/dev/null; then
        "$@" 2>>"${OBS_DIAGNOSTICS:?}" || true
    fi
}

if [[ ${SLEDRA_ENABLED:-0} == 1 && -f ${SLEDRA_CONTEXT:-} ]]; then
    SLEDRA_ATTEMPT_ACTIVE=1
    SLEDRA_ATTEMPT_CONTEXT=$SLEDRA_CONTEXT
fi

observe sledra_attempt_started "${EXECUTION_TOKEN:?}" "${task}_program"
if [[ $task == forecast ]]; then
    SLEDRA_CONFIG=$STAGE_CONFIG observe sledra_event_file stage.started "$job_dir/stage-start.json"
fi

"$job_dir/payload.sh" "$task"
business_rc=$?

if [[ $task == forecast ]]; then
    if [[ $business_rc == 0 ]]; then
        stage_end=stage-end-success.json
    else
        stage_end=stage-end-failure.json
    fi
    SLEDRA_CONFIG=$STAGE_CONFIG observe sledra_event_file stage.ended "$job_dir/$stage_end"
fi
observe sledra_attempt_exited "$business_rc" "${task}_program"
printf '%s:%s:%s\n' "$task" "$EXECUTION_TOKEN" "$business_rc" >>"${CALLBACK_LOG:?}"
exit "$business_rc"
