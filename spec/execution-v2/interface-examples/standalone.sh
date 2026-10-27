#!/usr/bin/env bash
# P0 interface example: requires the P1 library, or the explicit test-only mock.
# This script owns its options, traps and business command; the library does not.
set -euo pipefail

source "${SLEDRA_BASH_LIBRARY:?set the installed execution library path}"
sledra_execution_begin --task example.forecast
example_signal=0

on_exit() {
    local saved_rc=$?
    trap - EXIT
    if [[ $example_signal == 0 ]]; then
        sledra_execution_end --exit-code "$saved_rc"
    fi
    printf 'exit:%s\n' "$saved_rc" >> "$EXAMPLE_CALLBACK_LOG" || :
    exit "$saved_rc"
}

on_term() {
    example_signal=15
    sledra_execution_end --signal 15
    printf 'signal:TERM\n' >> "$EXAMPLE_CALLBACK_LOG" || :
    exit 143
}

trap on_exit EXIT
trap on_term TERM

# These are synthetic application settings, parsed after the public entry.
"${EXAMPLE_CONFIGURE:?set an application configuration check}"
: "${EXAMPLE_CYCLE:?set the actual business cycle}"
if [[ $# == 0 ]]; then
    printf 'missing business command\n' >&2
    exit 2
fi

sledra_execution_context --task example.forecast \
    --cycle-kind single --cycle "$EXAMPLE_CYCLE" \
    --int resolution_km 25 --string member 01
sledra_stage_begin forecast
sledra_execution_event forecast.progress \
    --float fraction 0.125 --string message "${EXAMPLE_MESSAGE:-forecast starts}"

# Keep the business command here, outside observation conditions/substitutions.
"$@"
saved_rc=$?
sledra_stage_end --exit-code "$saved_rc"
sledra_execution_end --exit-code "$saved_rc"
