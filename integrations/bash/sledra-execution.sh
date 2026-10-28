#!/usr/bin/env bash
# Source-only Bash >=4.3 lifecycle API. No options, traps or work are installed.
# Private state belongs to this BASHPID; every assignment explicitly removes
# export even when the caller uses set -a. Source itself only defines functions.

_sledra_v2_owner() {
    [[ ${BASH_SUBSHELL:-0} == 0 ]] || return 1
    local +x _name _decl _inherited=0
    for _name in "${!_SLEDRA_V2_@}"; do
        _decl=$(declare -p "$_name" 2>/dev/null) || continue
        if [[ $_decl =~ ^declare\ -[^\ ]*x ]]; then _inherited=1; break; fi
    done
    if [[ $_inherited == 1 ]]; then
        for _name in "${!_SLEDRA_V2_@}"; do unset -v "$_name"; done
    fi
    if [[ -n ${_SLEDRA_V2_OWNER:-} && $_SLEDRA_V2_OWNER != "$BASHPID" ]]; then return 1; fi
    if [[ -z ${_SLEDRA_V2_OWNER:-} ]]; then
        declare -g _SLEDRA_V2_OWNER="$BASHPID" _SLEDRA_V2_DIAG=0
        export -n _SLEDRA_V2_OWNER _SLEDRA_V2_DIAG
    fi
    return 0
}

_sledra_v2_diag() {
    if [[ ${_SLEDRA_V2_DIAG:-0} != 1 ]]; then
        declare -g _SLEDRA_V2_DIAG=1
        export -n _SLEDRA_V2_DIAG
        printf '{"ok":false,"code":"execution_observation_unavailable"}\n' >&2 || :
    fi
    return 0
}

_sledra_v2_disable() {
    if [[ ${_SLEDRA_V2_STATE:-} != closed ]]; then
        declare -g _SLEDRA_V2_STATE=disabled
        export -n _SLEDRA_V2_STATE
    fi
    _sledra_v2_diag
    return 0
}

# Capture at most the receipt limit plus status framing, preserving the final LF.
# read returns success on NUL or the byte limit; either makes the reply invalid.
# The process substitution runs only the recorder. It never runs business work.
_sledra_v2_call() {
    local +x LC_ALL=C _response _status
    if IFS= read -r -d '' -n 65799 _response < <(
        if "$_SLEDRA_V2_BIN" execution "$@" 2>/dev/null; then
            printf '\0360'
        else
            printf '\036%s' "$?"
        fi
    ); then
        _sledra_v2_disable
        return 1
    fi
    _status=${_response##*$'\036'}
    _response=${_response%$'\036'*}
    if [[ $_status == 2 && -z $_response ]]; then
        _sledra_v2_diag
        return 2
    fi
    if [[ $_status != 0 || ${#_response} -gt 65792 ]]; then
        _sledra_v2_disable
        return 1
    fi
    declare -g _SLEDRA_V2_REPLY="$_response"
    export -n _SLEDRA_V2_REPLY
    return 0
}

# Check only public option arities; Go owns validation/encoding and duplicates.
# Values that look like options remain literal values at their fixed positions.
_sledra_v2_public() {
    local +x _kind=$1 _opt _arity
    shift
    while [[ $# -gt 0 ]]; do
        _opt=$1 _arity=1
        case "$_kind:$_opt" in
            start:--task|start:--engine-kind|start:--engine-service|start:--engine-node|start:--engine-alias|\
            context:--task|context:--cycle-kind|context:--cycle|context:--scope|end:--exit-code|end:--signal)
                _arity=2 ;;
            start:--task=*|start:--engine-kind=*|start:--engine-service=*|start:--engine-node=*|start:--engine-alias=*|\
            context:--task=*|context:--cycle-kind=*|context:--cycle=*|context:--scope=*|end:--exit-code=*|end:--signal=*) ;;
            context:--string|context:--int|context:--float|context:--bool|\
            event:--string|event:--int|event:--float|event:--bool) _arity=3 ;;
            context:--null|event:--null) _arity=2 ;;
            *) _sledra_v2_diag; return 1 ;;
        esac
        if [[ $# -lt $_arity ]]; then _sledra_v2_diag; return 1; fi
        shift "$_arity"
    done
    return 0
}

_sledra_v2_prepare() {
    local +x _kind=$1 _id _aux _json _extra LC_ALL=C
    shift
    if _sledra_v2_call prepare "$_kind" "$@"; then :; else return $?; fi
    if [[ $_SLEDRA_V2_REPLY != *$'\n' || $_SLEDRA_V2_REPLY == *$'\r'* ]]; then
        _sledra_v2_disable; return 1
    fi
    # Strip exactly one LF; reject all remaining raw control bytes and extra TSV.
    _json=${_SLEDRA_V2_REPLY%$'\n'}
    if [[ $_json == *$'\n'* ]]; then _sledra_v2_disable; return 1; fi
    IFS=$'\t' read -r _id _aux _json _extra <<< "$_json"
    if [[ ! $_id =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ||
          -n $_extra || -z $_json || ${#_json} -gt 65536 || $_json == *[[:cntrl:]]* ||
          $_json != \{*\} ]]; then
        _sledra_v2_disable; return 1
    fi
    # read treats TAB as IFS whitespace; require the exact reconstruction too.
    if [[ $_SLEDRA_V2_REPLY != "$_id"$'\t'"$_aux"$'\t'"$_json"$'\n' ]]; then
        _sledra_v2_disable; return 1
    fi
    if [[ $_kind == start ]]; then
        if [[ ! $_aux =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z$ ]]; then
            _sledra_v2_disable; return 1
        fi
        declare -g _SLEDRA_V2_ID="$_id" _SLEDRA_V2_ENTRY="$_aux"
        export -n _SLEDRA_V2_ID _SLEDRA_V2_ENTRY
    else
        if [[ $_id != "$_SLEDRA_V2_ID" ]]; then _sledra_v2_disable; return 1; fi
        if [[ $_kind == stage-start ]]; then
            if [[ ! $_aux =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]; then
                _sledra_v2_disable; return 1
            fi
        elif [[ $_aux != - ]]; then
            _sledra_v2_disable; return 1
        fi
    fi
    declare -g _SLEDRA_V2_PENDING="$_json" _SLEDRA_V2_AUX="$_aux"
    export -n _SLEDRA_V2_PENDING _SLEDRA_V2_AUX
    return 0
}

_sledra_v2_append() {
    if _sledra_v2_call append --root "$_SLEDRA_V2_ROOT" --execution-id "$_SLEDRA_V2_ID" \
        --entry-time "$_SLEDRA_V2_ENTRY" "$@" <<< "$_SLEDRA_V2_PENDING"; then
        if [[ -z $_SLEDRA_V2_REPLY ]]; then return 0; fi
    fi
    # A prepared, cached record failing append is terminal, including an
    # unexpected validation refusal. Never retry a potentially written fact.
    _sledra_v2_disable
    return 1
}

_sledra_v2_active() {
    if ! _sledra_v2_owner; then return 1; fi
    if [[ ${_SLEDRA_V2_STATE:-} == active ]]; then return 0; fi
    if [[ -z ${_SLEDRA_V2_STATE:-} && -n ${SLEDRA_MODE:-} && ${SLEDRA_MODE:-} != off ]]; then
        _sledra_v2_diag
    fi
    return 1
}

sledra_execution_begin() {
    if ! _sledra_v2_owner; then return 0; fi
    if [[ -n ${_SLEDRA_V2_STATE:-} ]]; then return 0; fi
    declare -g _SLEDRA_V2_BIN="${SLEDRA_BIN:-}" _SLEDRA_V2_ROOT="${SLEDRA_ROOT:-}"
    export -n _SLEDRA_V2_BIN _SLEDRA_V2_ROOT
    case ${SLEDRA_MODE:-off} in
        ''|off)
            declare -g _SLEDRA_V2_STATE=off
            export -n _SLEDRA_V2_STATE
            return 0 ;;
        execution-v2) ;;
        *) _sledra_v2_disable; return 0 ;;
    esac
    declare -g _SLEDRA_V2_STATE=disabled
    export -n _SLEDRA_V2_STATE
    if [[ ${SLEDRA_ENABLED:-0} == 1 || $_SLEDRA_V2_BIN != /* || ! -f $_SLEDRA_V2_BIN ||
          ! -x $_SLEDRA_V2_BIN || $_SLEDRA_V2_ROOT != /* ]]; then
        _sledra_v2_diag; return 0
    fi
    if ! _sledra_v2_public start "$@"; then return 0; fi
    if ! _sledra_v2_prepare start "$@"; then return 0; fi
    if ! _sledra_v2_append --create; then return 0; fi
    declare -g _SLEDRA_V2_STATE=active
    export -n _SLEDRA_V2_STATE
    return 0
}

sledra_execution_context() {
    if ! _sledra_v2_active; then return 0; fi
    if ! _sledra_v2_public context "$@"; then return 0; fi
    if [[ -n ${_SLEDRA_V2_CONTEXT:-} ]]; then
        if ! _sledra_v2_prepare context --execution-id "$_SLEDRA_V2_ID" "$@" --previous-event "$_SLEDRA_V2_CONTEXT"; then return 0; fi
        if [[ $_SLEDRA_V2_PENDING != "$_SLEDRA_V2_CONTEXT" ]]; then _sledra_v2_disable; fi
        return 0
    fi
    if ! _sledra_v2_prepare context --execution-id "$_SLEDRA_V2_ID" "$@"; then return 0; fi
    if ! _sledra_v2_append; then return 0; fi
    declare -g _SLEDRA_V2_CONTEXT="$_SLEDRA_V2_PENDING"
    export -n _SLEDRA_V2_CONTEXT
    return 0
}

sledra_execution_event() {
    if ! _sledra_v2_active; then return 0; fi
    if [[ $# -lt 1 ]]; then _sledra_v2_diag; return 0; fi
    local +x _event_name=$1
    shift
    if ! _sledra_v2_public event "$@"; then return 0; fi
    if ! _sledra_v2_prepare event "$_event_name" --execution-id "$_SLEDRA_V2_ID" "$@"; then return 0; fi
    if ! _sledra_v2_append; then return 0; fi
    return 0
}

sledra_stage_begin() {
    if ! _sledra_v2_active; then return 0; fi
    if [[ $# != 1 || -n ${_SLEDRA_V2_STAGE:-} ]]; then _sledra_v2_diag; return 0; fi
    if ! _sledra_v2_prepare stage-start --execution-id "$_SLEDRA_V2_ID" --stage "$1"; then return 0; fi
    if ! _sledra_v2_append; then return 0; fi
    declare -g _SLEDRA_V2_STAGE="$1" _SLEDRA_V2_STAGE_ID="$_SLEDRA_V2_AUX"
    export -n _SLEDRA_V2_STAGE _SLEDRA_V2_STAGE_ID
    return 0
}

sledra_stage_end() {
    if ! _sledra_v2_active; then return 0; fi
    if [[ -z ${_SLEDRA_V2_STAGE:-} ]]; then _sledra_v2_diag; return 0; fi
    if ! _sledra_v2_public end "$@"; then return 0; fi
    if ! _sledra_v2_prepare stage-end --execution-id "$_SLEDRA_V2_ID" \
        --stage "$_SLEDRA_V2_STAGE" --stage-id "$_SLEDRA_V2_STAGE_ID" "$@"; then return 0; fi
    if ! _sledra_v2_append; then return 0; fi
    unset -v _SLEDRA_V2_STAGE _SLEDRA_V2_STAGE_ID
    return 0
}

sledra_execution_end() {
    if ! _sledra_v2_owner; then return 0; fi
    if [[ ${_SLEDRA_V2_STATE:-} != closed ]]; then
        if ! _sledra_v2_active; then return 0; fi
    fi
    if ! _sledra_v2_public end "$@"; then return 0; fi
    if [[ -n ${_SLEDRA_V2_END:-} ]]; then
        if ! _sledra_v2_prepare end --execution-id "$_SLEDRA_V2_ID" "$@" --previous-event "$_SLEDRA_V2_END"; then return 0; fi
        if [[ $_SLEDRA_V2_PENDING != "$_SLEDRA_V2_END" ]]; then _sledra_v2_disable; fi
        return 0
    fi
    if ! _sledra_v2_prepare end --execution-id "$_SLEDRA_V2_ID" "$@"; then return 0; fi
    if ! _sledra_v2_append; then return 0; fi
    declare -g _SLEDRA_V2_END="$_SLEDRA_V2_PENDING" _SLEDRA_V2_STATE=closed
    export -n _SLEDRA_V2_END _SLEDRA_V2_STATE
    return 0
}

# set -a also marks function definitions for export. Keep these definitions
# source-only even in that caller mode; do not export a child recording service.
export -nf _sledra_v2_owner _sledra_v2_diag _sledra_v2_disable _sledra_v2_call \
    _sledra_v2_public _sledra_v2_prepare _sledra_v2_append _sledra_v2_active \
    sledra_execution_begin sledra_execution_context sledra_execution_end \
    sledra_execution_event sledra_stage_begin sledra_stage_end
