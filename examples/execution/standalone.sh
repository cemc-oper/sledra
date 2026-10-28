#!/usr/bin/env bash
# This application owner controls options, traps, callbacks and business work.
set -euo pipefail

example_source=${BASH_SOURCE[0]}
[[ $example_source == */* ]] || example_source=./$example_source
source "${SLEDRA_BASH_LIBRARY:-${example_source%/*}/../../integrations/bash/sledra-execution.sh}"
sledra_execution_begin --task example.command
example_signal=0
example_business_started=0

example_callback() {
    if [[ -n ${EXAMPLE_CALLBACK_LOG:-} ]]; then
        printf '%s\n' "$1" >> "$EXAMPLE_CALLBACK_LOG" || :
    fi
    return 0
}

on_exit() {
    local saved_rc=$?
    trap - EXIT
    if [[ $example_signal != 0 ]]; then
        if [[ $example_business_started == 1 ]]; then
            sledra_stage_end --signal "$example_signal"
        fi
        sledra_execution_end --signal "$example_signal"
    else
        if [[ $example_business_started == 1 ]]; then
            sledra_stage_end --exit-code "$saved_rc"
        fi
        sledra_execution_end --exit-code "$saved_rc"
    fi
    example_callback "exit:$saved_rc"
    # Optional application callback policy: the recorded result remains saved_rc.
    if [[ ${EXAMPLE_CALLBACK_EXIT_ZERO:-0} == 1 ]]; then exit 0; fi
    exit "$saved_rc"
}

on_term() {
    example_signal=15
    example_callback signal:TERM
    exit 143
}

trap on_exit EXIT
trap on_term TERM

# An optional application configuration check runs after the public entry.
if [[ -n ${EXAMPLE_CONFIGURE:-} ]]; then
    "$EXAMPLE_CONFIGURE"
fi
if [[ $# == 0 ]]; then
    printf 'usage: standalone.sh COMMAND [ARG ...]\n' >&2
    exit 2
fi

# This generic command example explicitly defaults to no business cycle.
# Applications supply actual effective values; no scheduler date is inferred.
context_args=(--task example.command --cycle-kind "${EXAMPLE_CYCLE_KIND:-none}")
case ${EXAMPLE_CYCLE_KIND:-none} in
    single)
        if [[ -z ${EXAMPLE_CYCLE:-} ]]; then
            printf 'single cycle requires EXAMPLE_CYCLE\n' >&2
            exit 2
        fi
        context_args+=(--cycle "$EXAMPLE_CYCLE") ;;
    none|multiple)
        if [[ -n ${EXAMPLE_CYCLE:-} ]]; then
            printf 'none/multiple cycle must omit EXAMPLE_CYCLE\n' >&2
            exit 2
        fi ;;
    *) printf 'invalid EXAMPLE_CYCLE_KIND\n' >&2; exit 2 ;;
esac
sledra_execution_context "${context_args[@]}" --int resolution_km 25 --string member 01
sledra_stage_begin command
sledra_execution_event example.progress --float fraction 0.125 \
    --string message "${EXAMPLE_MESSAGE:-command starts}"

example_business_started=1
# Keep the original business command here. Do not wrap it in an observation
# condition, launcher, pipeline or command substitution that changes errexit.
"$@"
business_rc=$?
exit "$business_rc"
