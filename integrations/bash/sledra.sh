#!/usr/bin/env bash
# Source-only Bash API. It does not set options, install traps, or run payloads.

_sledra_shell_diag_once() {
    if [[ ${SLEDRA_SHELL_DIAG_EMITTED:-0} != 1 ]]; then
        SLEDRA_SHELL_DIAG_EMITTED=1
        printf '{"ok":false,"code":"shell_observation_unavailable"}\n' >&2 || :
    fi
    return 0
}

_sledra_shell_ready() {
    [[ ${SLEDRA_ENABLED:-0} == 1 ]] || return 1
    if [[ ${SLEDRA_BIN:-} != /* || ! -x ${SLEDRA_BIN:-} || ! -f ${SLEDRA_BIN:-} ||
          ${SLEDRA_ROOT:-} != /* ||
          ( -n ${SLEDRA_SPEC_DIR:-} && ${SLEDRA_SPEC_DIR:-} != /* ) || ${SLEDRA_CONFIG:-} != /* ]]; then
        _sledra_shell_diag_once
        return 1
    fi
    return 0
}

_sledra_shell_call() {
    # The installed binary is trusted, but none of its output enters business
    # stdout/stderr. The fixed diagnostic above has no input content.
    "${SLEDRA_BIN}" "$@" >/dev/null 2>/dev/null
}

sledra_run_begin() {
    SLEDRA_RUN_ACTIVE=0
    SLEDRA_ATTEMPT_ACTIVE=0
    SLEDRA_ATTEMPT_CONTEXT=
    if ! _sledra_shell_ready; then return 0; fi
    if [[ $# != 2 || -z ${1:-} || -z ${2:-} ]]; then _sledra_shell_diag_once; return 0; fi
    if _sledra_shell_call registry run-create --request-file "$1" --context-out "$2"; then
        SLEDRA_RUN_ACTIVE=1
    else
        _sledra_shell_diag_once
    fi
    return 0
}

sledra_attempt_begin() {
    SLEDRA_ATTEMPT_ACTIVE=0
    SLEDRA_ATTEMPT_CONTEXT=
    if [[ ${SLEDRA_RUN_ACTIVE:-0} != 1 ]]; then return 0; fi
    if ! _sledra_shell_ready; then return 0; fi
    if [[ $# != 3 || -z ${1:-} || -z ${2:-} || -z ${3:-} ]]; then _sledra_shell_diag_once; return 0; fi
    if _sledra_shell_call registry attempt-create --context "$1" --request-file "$2" --context-out "$3"; then
        SLEDRA_ATTEMPT_ACTIVE=1
        SLEDRA_ATTEMPT_CONTEXT=$3
    else
        _sledra_shell_diag_once
    fi
    return 0
}

_sledra_shell_event() {
    if [[ ${SLEDRA_ATTEMPT_ACTIVE:-0} != 1 ]]; then return 0; fi
    if ! _sledra_shell_ready; then return 0; fi
    if ! _sledra_shell_call event --context "${SLEDRA_ATTEMPT_CONTEXT}" "$@" --strict; then
        _sledra_shell_diag_once
    fi
    return 0
}

sledra_event_file() {
    if [[ ${SLEDRA_ATTEMPT_ACTIVE:-0} != 1 ]]; then return 0; fi
    if [[ $# != 2 || -z ${1:-} || -z ${2:-} ]]; then _sledra_shell_diag_once; return 0; fi
    _sledra_shell_event --type "$1" --payload-file "$2"
}

sledra_attempt_started() {
    if [[ ${SLEDRA_ATTEMPT_ACTIVE:-0} != 1 ]]; then return 0; fi
    if [[ $# != 2 || -z ${1:-} || -z ${2:-} ]]; then _sledra_shell_diag_once; return 0; fi
    _sledra_shell_event --type attempt.started --invocation-id "$1" --process-role "$2"
}

sledra_attempt_exited() {
    if [[ ${SLEDRA_ATTEMPT_ACTIVE:-0} != 1 ]]; then return 0; fi
    if [[ $# != 2 || -z ${1:-} || -z ${2:-} ]]; then _sledra_shell_diag_once; return 0; fi
    _sledra_shell_event --type attempt.exited --process-role "$2" --exit-code "$1"
}

sledra_attempt_signaled() {
    if [[ ${SLEDRA_ATTEMPT_ACTIVE:-0} != 1 ]]; then return 0; fi
    if [[ $# != 2 || -z ${1:-} || -z ${2:-} ]]; then _sledra_shell_diag_once; return 0; fi
    _sledra_shell_event --type attempt.exited --process-role "$2" --signal "$1"
}

sledra_run_finish() {
    if [[ ${SLEDRA_RUN_ACTIVE:-0} != 1 ]]; then return 0; fi
    if ! _sledra_shell_ready; then return 0; fi
    if [[ $# != 2 || -z ${1:-} || -z ${2:-} ]]; then _sledra_shell_diag_once; return 0; fi
    if ! _sledra_shell_call run finish --context "$1" --scope-file "$2"; then
        _sledra_shell_diag_once
    fi
    return 0
}
