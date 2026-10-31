#!/usr/bin/env bash
# Source-only CE-1 API, Bash >=4.3. No trap, option, payload or source-time IO.
# Helpers use caller-local _ce_* values; persistent state is explicitly unexported.
_sledra_ce_process() {
    local +x _name _decl _reset=0
    for _name in "${!_SLEDRA_CE_@}"; do
        _decl=$(declare -p "$_name" 2>/dev/null) || continue
        if [[ $_decl =~ ^declare\ -[^\ ]*x ]]; then _reset=1; break; fi
    done
    if [[ $_reset == 1 || ${_SLEDRA_CE_PID:-} != "$BASHPID" ]]; then
        for _name in "${!_SLEDRA_CE_@}"; do unset -v "$_name"; done
        declare -g _SLEDRA_CE_PID="$BASHPID" _SLEDRA_CE_STATE=new _SLEDRA_CE_DIAG=0
        export -n _SLEDRA_CE_PID _SLEDRA_CE_STATE _SLEDRA_CE_DIAG
    fi
}

# The child recorder alone is in the process substitution. Capture status and
# trailing LF, reject NUL/oversize, and never expose its stdout/stderr to business.
_sledra_ce_call() {
    local +x LC_ALL=C _wire _private
    _ce_reply= _ce_rc=invalid
    # set -a re-exports even a previously unexported local on reassignment.
    # Strip every caller-local transport/cache value before spawning the CLI.
    for _private in _ce_bin _ce_ref _ce_id _ce_input _ce_reply _ce_rc _ce_dead \
        _ce_pending _ce_request _ce_event _ce_digest _ce_aux _ce_outcome _ce_role \
        _ce_owner_key _ce_result _ce_code _ce_rrequest _ce_revent _ce_phase \
        _ce_stop _ce_receipt_aux _ce_signature; do export -n "$_private"; done
    export -n _private
    if IFS= read -r -d '' -n 66056 _wire < <(
        if "$_ce_bin" execution child-events "$@" <<< "${_ce_input%$'\n'}" 2>/dev/null; then
            printf '\0360'
        else
            printf '\036%s' "$?"
        fi
    ); then return 1; fi
    [[ $_wire == *$'\036'* ]] || return 1
    _ce_rc=${_wire##*$'\036'}
    _ce_reply=${_wire%$'\036'*}
    [[ $_ce_rc =~ ^(0|2[0-6])$ && $_ce_reply != *$'\036'* && ${#_ce_reply} -le 66048 ]]
}

_sledra_ce_diag() {
    [[ $_SLEDRA_CE_DIAG != 1 ]] || return 0
    declare -g _SLEDRA_CE_DIAG=1
    export -n _SLEDRA_CE_DIAG
    if [[ -n $_ce_ref ]]; then
        _ce_input=
        if _sledra_ce_call diagnostic --context "$_ce_ref" && [[ $_ce_rc == 0 ]]; then
            case $_ce_reply in
                $'CE-1\tdiagnostic\tsuppressed\n') return 0 ;;
                $'CE-1\tdiagnostic\tgranted\n') ;;
                *) ;; # Damaged diagnostic transport uses the local budget.
            esac
        fi
    fi
    printf '{"ok":false,"code":"execution_observation_unavailable"}\n' >&2 || :
    return 0
}

_sledra_ce_handshake() {
    [[ $_ce_bin == /* && -f $_ce_bin && -x $_ce_bin ]] || return 1
    _sledra_ce_call capabilities --interface CE-1 --protocol 1 &&
        [[ $_ce_rc == 0 && $_ce_reply == $'CE-1\texecution-child-events\t1\n' ]]
}

_sledra_ce_uuid() {
    [[ $1 =~ ^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$ ]]
}

_sledra_ce_reference() {
    local +x LC_ALL=C _path _session
    [[ ${#1} -le 2048 && $1 == sledra-ce1:/*/event-context.json && $1 != *[[:cntrl:]]* ]] || return 1
    _path=${1#sledra-ce1:} _path=${_path%/event-context.json}
    [[ $_path != *//* && $_path != */./* && $_path != */../* && $_path != */. && $_path != */.. ]] || return 1
    _session=${_path##*/}
    _sledra_ce_uuid "$_session"
}

_sledra_ce_prepare() {
    local +x _kind=$1 _v _execution _session _key _json _extra LC_ALL=C
    shift
    if ! _sledra_ce_call prepare "$_kind" "$@"; then _ce_dead=1; _sledra_ce_diag; return 1; fi
    if [[ $_ce_rc == 20 ]]; then
        if _sledra_ce_receipt prepare; then _sledra_ce_diag; return 1; fi
    fi
    if [[ $_ce_rc != 0 || $_ce_reply != *$'\n' || $_ce_reply == *$'\r'* ]]; then
        _ce_dead=1; _sledra_ce_diag; return 1
    fi
    IFS=$'\t' read -r _v _ce_request _execution _ce_event _ce_aux _session _key _ce_digest _json _extra <<< "${_ce_reply%$'\n'}"
    if [[ $_v != 1 || -n $_extra || ${#_json} -gt 65536 || $_json != \{*\} || $_json == *[[:cntrl:]]* ||
          ! $_ce_digest =~ ^[0-9a-f]{64}$ ]] ||
        ! _sledra_ce_uuid "$_ce_request" || ! _sledra_ce_uuid "$_execution" || ! _sledra_ce_uuid "$_ce_event" ||
        [[ $_ce_reply != "1"$'\t'"$_ce_request"$'\t'"$_execution"$'\t'"$_ce_event"$'\t'"$_ce_aux"$'\t'"$_session"$'\t'"$_key"$'\t'"$_ce_digest"$'\t'"$_json"$'\n' ]]; then
        _ce_dead=1; _sledra_ce_diag; return 1
    fi
    if [[ $_kind == start ]]; then
        if ! _sledra_ce_uuid "$_session" || [[ ! $_key =~ ^[0-9a-f]{64}$ ||
            ! $_ce_aux =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z$ ]]; then
            _ce_dead=1; _sledra_ce_diag; return 1
        fi
        _ce_id=$_execution _ce_owner_key=$_key
    else
        if [[ $_execution != "$_ce_id" || $_session != - || $_key != - ]]; then
            _ce_dead=1; _sledra_ce_diag; return 1
        fi
        if [[ $_kind == stage-start ]]; then
            if ! _sledra_ce_uuid "$_ce_aux"; then _ce_dead=1; _sledra_ce_diag; return 1; fi
        elif [[ $_ce_aux != - ]]; then _ce_dead=1; _sledra_ce_diag; return 1; fi
    fi
    _ce_pending=$_ce_reply
    return 0
}

# Nine-field completion frame and exact status/code pairing. Operation-specific
# IDs/results are checked by the caller. Decimal comparison avoids Bash overflow.
_sledra_ce_receipt() {
    local +x _v _rev _aux _extra LC_ALL=C _command=${1:-commit}
    [[ ${#_ce_reply} -le 4096 && $_ce_reply == *$'\n' && $_ce_reply != *$'\r'* ]] || return 1
    IFS=$'\t' read -r _v _ce_result _ce_code _ce_rrequest _ce_revent _rev _ce_phase _ce_stop _aux _extra <<< "${_ce_reply%$'\n'}"
    [[ $_v == 1 && -z $_extra && $_ce_reply == "1"$'\t'"$_ce_result"$'\t'"$_ce_code"$'\t'"$_ce_rrequest"$'\t'"$_ce_revent"$'\t'"$_rev"$'\t'"$_ce_phase"$'\t'"$_ce_stop"$'\t'"$_aux"$'\n' ]] || return 1
    [[ $_ce_rrequest == - ]] || _sledra_ce_uuid "$_ce_rrequest" || return 1
    [[ $_ce_revent == - ]] || _sledra_ce_uuid "$_ce_revent" || return 1
    if [[ $_rev != - ]]; then
        [[ $_rev =~ ^(0|[1-9][0-9]{0,18})$ ]] || return 1
        [[ ${#_rev} -lt 19 || $_rev < 9223372036854775808 ]] || return 1
    fi
    case $_ce_phase in off|creating|active|writing|closing|closed|disabled|unavailable) ;; *) return 1 ;; esac
    case $_ce_stop in none|local|requested|quiescent) ;; *) return 1 ;; esac
    case "$_ce_rc:$_ce_result:$_ce_code" in
        0:committed:ok|0:noop:same_context|0:noop:same_end|0:attached:ok|0:confirmed:ok) ;;
        20:rejected:usage|20:rejected:invalid_value|20:rejected:duplicate_key|20:rejected:limit_exceeded|20:rejected:reserved_event_name|20:rejected:role_denied|20:rejected:stage_order|20:rejected:reference_switch) ;;
        21:busy:writer_busy|22:closed:closing|22:closed:closed|22:closed:conflicting_end) ;;
        23:disabled:session_disabled|23:disabled:indeterminate|23:disabled:control_unavailable|23:disabled:stale_writing|23:disabled:conflicting_context|23:disabled:disable_publish_failed) ;;
        24:unknown:result_unknown|25:incompatible:protocol_mismatch|25:incompatible:installation_incompatible|26:unavailable:invalid_reference|26:unavailable:identity_mismatch|26:unavailable:owner_gone) ;;
        *) return 1 ;;
    esac
    case $_ce_result in
        committed|confirmed|noop) [[ $_rev != - && $_ce_rrequest != - && $_ce_revent != - ]] || return 1 ;;
        disabled|unknown|incompatible|unavailable) [[ $_ce_stop != none ]] || return 1 ;;
    esac
    if [[ $_ce_stop == requested || $_ce_stop == quiescent ]]; then [[ $_ce_phase == disabled ]] || return 1; fi
    case $_ce_rc in
        21) [[ $_ce_stop == none && ( $_ce_phase == active || $_ce_phase == writing || $_ce_phase == closing ) ]] || return 1 ;;
        22) [[ $_ce_stop == none && ( $_ce_phase == closing || $_ce_phase == closed ) ]] || return 1 ;;
        23) [[ $_ce_phase == disabled || $_ce_phase == unavailable ]] || return 1 ;;
    esac
    case $_ce_result in
        noop) [[ ( $_ce_code == same_end && $_ce_phase == closed ) || ( $_ce_code == same_context && $_ce_phase == active ) ]] || return 1 ;;
        committed|confirmed) [[ $_ce_phase == active || $_ce_phase == closed || $_ce_phase == closing || $_ce_phase == disabled ]] || return 1 ;;
        attached) [[ ( $_command == attach || $_command == bind ) && $_ce_phase == active && $_ce_stop == none ]] || return 1 ;;
    esac
    if [[ $_ce_result == confirmed && $_command != result ]]; then return 1; fi
    if [[ $_command == open && $_ce_result == committed ]]; then
        _sledra_ce_reference "$_aux" || return 1
    elif [[ $_aux != - ]]; then return 1; fi
    _ce_receipt_aux=$_aux
    return 0
}

_sledra_ce_stop() {
    _ce_input=
    if [[ -n $_ce_ref ]]; then _sledra_ce_call disable --context "$_ce_ref" || :; fi
    _ce_dead=1
    _sledra_ce_diag
    return 0
}

_sledra_ce_commit() {
    _ce_input=$_ce_pending
    local +x -a _role=(--role event)
    if [[ $_ce_role == owner ]]; then _role=(--role owner --owner-key "$_ce_owner_key"); fi
    if _sledra_ce_call commit --context "$_ce_ref" "${_role[@]}" && _sledra_ce_receipt &&
        [[ $_ce_receipt_aux == - && ( $_ce_rrequest == "$_ce_request" || $_ce_rrequest == - ) &&
        ( $_ce_revent == "$_ce_event" || $_ce_revent == - ) ]]; then
        if [[ $_ce_rc == 0 && ( $_ce_rrequest != "$_ce_request" || $_ce_revent != "$_ce_event" ||
            ( $_ce_result != committed && $_ce_result != noop ) ) ]]; then :
        else
            case $_ce_rc in
                0)
                    if [[ $_ce_stop != none || $_ce_phase == disabled ]]; then _ce_dead=1; fi
                    _ce_outcome=$_ce_phase
                    return 0 ;;
                20) _sledra_ce_diag; return 1 ;;
                21) _ce_outcome=$_ce_phase; return 1 ;;
                22)
                    _ce_outcome=$_ce_phase
                    if [[ $_ce_code == conflicting_end ]]; then _sledra_ce_diag; fi
                    return 1 ;;
                *) _ce_dead=1; _sledra_ce_diag; return 1 ;;
            esac
        fi
    fi
    # One readonly lookup of this exact prepared request. Never prepare/commit it again.
    if _sledra_ce_call result --context "$_ce_ref" --request-id "$_ce_request" --event-id "$_ce_event" --record-sha256 "$_ce_digest" &&
        _sledra_ce_receipt result && [[ $_ce_rc == 0 && $_ce_result == confirmed && $_ce_rrequest == "$_ce_request" &&
        $_ce_revent == "$_ce_event" && $_ce_receipt_aux == - ]]; then
        _ce_outcome=$_ce_phase
        if [[ $_ce_stop != none || $_ce_phase == disabled ]]; then _ce_dead=1; _sledra_ce_diag; fi
        return 0
    fi
    _sledra_ce_stop
    return 1
}

_sledra_ce_event_impl() {
    local +x _action=$1
    shift
    _sledra_ce_process
    [[ $_SLEDRA_CE_STATE != disabled && $_SLEDRA_CE_STATE != off ]] || return 0
    local +x _ce_bin=${_SLEDRA_CE_BIN:-} _ce_ref=${_SLEDRA_CE_REF:-} _ce_id=${_SLEDRA_CE_ID:-}
    local +x _ce_input= _ce_reply _ce_rc _ce_dead=0 _ce_pending _ce_request _ce_event _ce_digest _ce_aux _ce_outcome=active _ce_role=event _ce_owner_key= _ce_signature
    local +x _ce_result _ce_code _ce_rrequest _ce_revent _ce_phase _ce_stop _ce_receipt_aux _ref
    case $_action in
        detach)
            if [[ $# != 0 ]]; then _sledra_ce_diag; return 0; fi
            unset -v _SLEDRA_CE_REF _SLEDRA_CE_ID
            return 0 ;;
        attach)
            if [[ $_SLEDRA_CE_STATE == new ]]; then
                declare -g _SLEDRA_CE_BIN="${SLEDRA_BIN:-}" _SLEDRA_CE_STATE=on
                export -n _SLEDRA_CE_BIN _SLEDRA_CE_STATE
                _ce_bin=$_SLEDRA_CE_BIN
                case ${SLEDRA_MODE:-off} in
                    ''|off) declare -g _SLEDRA_CE_STATE=off; export -n _SLEDRA_CE_STATE; return 0 ;;
                    execution-v2) ;;
                    *) _ce_dead=1 ;;
                esac
                if [[ ${SLEDRA_ENABLED:-0} == 1 ]]; then _ce_dead=1; fi
            fi
            if [[ $_ce_dead != 1 ]]; then
                _ref=${SLEDRA_EVENT_CONTEXT:-}
                if [[ $# != 0 ]]; then
                    if [[ $# != 2 || $1 != --context ]]; then _sledra_ce_diag; return 0; fi
                    _ref=$2
                fi
                if [[ -z $_ref ]]; then return 0; fi
                if [[ ${_SLEDRA_CE_HANDSHAKE:-0} != 1 ]]; then
                    if ! _sledra_ce_handshake; then
                        declare -g _SLEDRA_CE_STATE=disabled
                        export -n _SLEDRA_CE_STATE
                        _sledra_ce_diag
                        return 0
                    fi
                    declare -g _SLEDRA_CE_HANDSHAKE=1
                    export -n _SLEDRA_CE_HANDSHAKE
                fi
                if [[ -n $_ce_ref && $_ce_ref != "$_ref" ]]; then _sledra_ce_diag; return 0; fi
                if ! _sledra_ce_reference "$_ref"; then _sledra_ce_diag; return 0; fi
                if _sledra_ce_call bind --context "$_ref" && [[ $_ce_rc == 0 && $_ce_reply == $'CE-1\tbound\t'*$'\n' ]]; then
                    _ce_id=${_ce_reply#$'CE-1\tbound\t'} _ce_id=${_ce_id%$'\n'}
                    if _sledra_ce_uuid "$_ce_id"; then
                        declare -g _SLEDRA_CE_REF="$_ref" _SLEDRA_CE_ID="$_ce_id"
                        export -n _SLEDRA_CE_REF _SLEDRA_CE_ID
                        return 0
                    fi
                elif _sledra_ce_receipt bind && [[ $_ce_receipt_aux == - && $_ce_rrequest == - && $_ce_revent == - ]]; then
                    if [[ $_ce_rc == 21 || $_ce_rc == 22 ]]; then return 0; fi
                    if [[ -z $_ce_ref && $_ce_rc == 26 ]]; then
                        _ce_ref=$_ref; _sledra_ce_diag; return 0
                    fi
                fi
                if [[ -z $_ce_ref ]]; then _ce_ref=$_ref; fi
                _ce_dead=1
            fi ;;
        emit)
            [[ -n $_ce_ref ]] || return 0
            if [[ $# == 0 ]]; then _sledra_ce_diag; return 0; fi
            _ref=$1; shift
            # Only typed values; identities/roles/raw JSON never cross the public API.
            if ! _sledra_ce_typed "$@"; then _sledra_ce_diag; return 0; fi
            if _sledra_ce_prepare event "$_ref" --execution-id "$_ce_id" "$@"; then _sledra_ce_commit || :; fi ;;
    esac
    if [[ $_ce_dead == 1 ]]; then
        declare -g _SLEDRA_CE_STATE=disabled
        export -n _SLEDRA_CE_STATE
        _sledra_ce_diag
    fi
    return 0
}

_sledra_ce_typed() {
    local +x _arity
    while [[ $# -gt 0 ]]; do
        case $1 in
            --string|--int|--float|--bool) _arity=3 ;;
            --null) _arity=2 ;;
            *) return 1 ;;
        esac
        [[ $# -ge $_arity ]] || return 1
        shift "$_arity"
    done
}

sledra_event_attach() {
    local +x _trace=0
    case $- in *x*) set +x; _trace=1 ;; esac
    _sledra_ce_event_impl attach "$@"
    if [[ $_trace == 1 ]]; then set -x; fi
    return 0
}
sledra_event_emit() {
    local +x _trace=0
    case $- in *x*) set +x; _trace=1 ;; esac
    _sledra_ce_event_impl emit "$@"
    if [[ $_trace == 1 ]]; then set -x; fi
    return 0
}
sledra_event_detach() {
    local +x _trace=0
    case $- in *x*) set +x; _trace=1 ;; esac
    _sledra_ce_event_impl detach "$@"
    if [[ $_trace == 1 ]]; then set -x; fi
    return 0
}

export -nf _sledra_ce_process _sledra_ce_call _sledra_ce_diag _sledra_ce_handshake \
    _sledra_ce_uuid _sledra_ce_reference _sledra_ce_prepare _sledra_ce_receipt \
    _sledra_ce_stop _sledra_ce_commit _sledra_ce_event_impl _sledra_ce_typed \
    sledra_event_attach sledra_event_emit sledra_event_detach

_sledra_ce_owner_begin() {
    _sledra_ce_process
    local +x _ce_bin=$_SLEDRA_V2_BIN _ce_ref= _ce_input= _ce_reply _ce_rc _ce_dead=0 _ce_id _ce_owner_key
    local +x _ce_pending _ce_request _ce_event _ce_digest _ce_aux _ce_result _ce_code _ce_rrequest _ce_revent _ce_phase _ce_stop _ce_receipt_aux _ce_outcome _ce_role _ce_signature
    local +x _count=0 _opt _runtime=${SLEDRA_RUNTIME_DIR:-}
    local +x -a _args=()
    declare -g _SLEDRA_V2_CHILD=1
    export -n _SLEDRA_V2_CHILD
    while [[ $# -gt 0 ]]; do
        _opt=$1; shift
        case $_opt in
            --child-events) _count=$((_count + 1)) ;;
            --task|--engine-kind|--engine-service|--engine-node|--engine-alias)
                if [[ $# == 0 ]]; then _sledra_ce_diag; return 0; fi
                _args+=("$_opt" "$1"); shift ;;
            *) _args+=("$_opt") ;;
        esac
    done
    if [[ $_count != 1 || $_runtime != /* ]] || ! _sledra_ce_handshake; then _sledra_ce_diag; return 0; fi
    if ! _sledra_ce_prepare start "${_args[@]}"; then return 0; fi
    _ce_input=$_ce_pending
    if _sledra_ce_call open --root "$_SLEDRA_V2_ROOT" --runtime-dir "$_runtime" --owner-pid "$BASHPID" &&
        _sledra_ce_receipt open && [[ $_ce_rc == 0 && $_ce_result == committed && $_ce_code == ok &&
        $_ce_rrequest == "$_ce_request" && $_ce_revent == "$_ce_event" && $_ce_phase == active &&
        $_ce_stop == none && $_ce_receipt_aux != - ]]; then
        _ce_ref=$_ce_receipt_aux
        declare -g _SLEDRA_V2_CE_REF="$_ce_ref" _SLEDRA_V2_ID="$_ce_id" _SLEDRA_V2_CE_KEY="$_ce_owner_key"
        declare -g _SLEDRA_V2_STATE=active _SLEDRA_V2_CE_RUNTIME="$_runtime"
        export -n _SLEDRA_V2_CE_REF _SLEDRA_V2_ID _SLEDRA_V2_CE_KEY _SLEDRA_V2_STATE _SLEDRA_V2_CE_RUNTIME
        export SLEDRA_EVENT_CONTEXT="$_ce_ref"
    else
        # Never search for an open whose reference was not safely delivered.
        _sledra_ce_diag
    fi
    return 0
}

_sledra_ce_end_signature() {
    local +x _value _option _argument _budget=0 LC_ALL=C
    for _argument in "$@"; do _budget=$((_budget + ${#_argument} + 1)); done
    [[ $_budget -le 131072 ]] || return 1
    if [[ $# == 2 ]]; then _option=$1 _value=$2
    elif [[ $# == 1 && $1 == *=* ]]; then _option=${1%%=*} _value=${1#*=}
    else return 1; fi
    case $_option in --exit-code|--signal) ;; *) return 1 ;; esac
    [[ $_value =~ ^[+-]?[0-9]+$ ]] || return 1
    local +x _negative=0
    if [[ $_value == -* ]]; then _negative=1; fi
    _value=${_value#[+-]}
    _value=${_value#"${_value%%[!0]*}"}
    _value=${_value:-0}
    [[ ${#_value} -le 3 && ( $_negative == 0 || $_value == 0 ) ]] || return 1
    if [[ $_option == --exit-code ]]; then [[ $_value -le 255 ]] || return 1
    else [[ $_value -ge 1 && $_value -le 127 ]] || return 1; fi
    _ce_signature=$_option:$_value
}

_sledra_ce_owner_hook() {
    local +x _kind=$1
    shift
    [[ $_SLEDRA_V2_STATE != disabled ]] || return 0
    _sledra_ce_process
    local +x _ce_bin=$_SLEDRA_V2_BIN _ce_ref=$_SLEDRA_V2_CE_REF _ce_id=$_SLEDRA_V2_ID _ce_owner_key=$_SLEDRA_V2_CE_KEY
    local +x _ce_input= _ce_reply _ce_rc _ce_dead=0 _ce_pending _ce_request _ce_event _ce_digest _ce_aux _ce_outcome=active _ce_role=owner _ce_signature
    local +x _ce_result _ce_code _ce_rrequest _ce_revent _ce_phase _ce_stop _ce_receipt_aux _name
    if [[ $_kind != end && $_SLEDRA_V2_STATE != active ]]; then return 0; fi
    case $_kind in
        context)
            if ! _sledra_v2_public context "$@"; then return 0; fi
            if _sledra_ce_prepare context --execution-id "$_ce_id" "$@"; then _sledra_ce_commit || :; fi ;;
        event)
            if [[ $# == 0 ]]; then _sledra_ce_diag; return 0; fi
            _name=$1; shift
            if ! _sledra_ce_typed "$@"; then _sledra_ce_diag; return 0; fi
            if _sledra_ce_prepare event "$_name" --execution-id "$_ce_id" "$@"; then _sledra_ce_commit || :; fi ;;
        stage-start)
            if [[ $# != 1 || -n ${_SLEDRA_V2_STAGE:-} ]]; then _sledra_ce_diag; return 0; fi
            if _sledra_ce_prepare stage-start --execution-id "$_ce_id" --stage "$1" && _sledra_ce_commit; then
                declare -g _SLEDRA_V2_STAGE="$1" _SLEDRA_V2_STAGE_ID="$_ce_aux"
                export -n _SLEDRA_V2_STAGE _SLEDRA_V2_STAGE_ID
            fi ;;
        stage-end)
            if [[ -z ${_SLEDRA_V2_STAGE:-} ]]; then _sledra_ce_diag; return 0; fi
            if ! _sledra_v2_public end "$@"; then return 0; fi
            if _sledra_ce_prepare stage-end --execution-id "$_ce_id" --stage "$_SLEDRA_V2_STAGE" --stage-id "$_SLEDRA_V2_STAGE_ID" "$@" && _sledra_ce_commit; then
                unset -v _SLEDRA_V2_STAGE _SLEDRA_V2_STAGE_ID
            fi ;;
        end)
            if ! _sledra_ce_end_signature "$@"; then _sledra_ce_diag; return 0; fi
            if [[ -n ${_SLEDRA_V2_CE_END:-} ]]; then
                if [[ $_ce_signature != "$_SLEDRA_V2_CE_END_SIGNATURE" ]]; then _sledra_ce_diag; return 0; fi
                _ce_pending=$_SLEDRA_V2_CE_END _ce_request=$_SLEDRA_V2_CE_END_REQUEST
                _ce_event=$_SLEDRA_V2_CE_END_EVENT _ce_digest=$_SLEDRA_V2_CE_END_DIGEST
            elif _sledra_ce_prepare end --execution-id "$_ce_id" "$@"; then
                declare -g _SLEDRA_V2_CE_END="$_ce_pending" _SLEDRA_V2_CE_END_SIGNATURE="$_ce_signature"
                declare -g _SLEDRA_V2_CE_END_REQUEST="$_ce_request" _SLEDRA_V2_CE_END_EVENT="$_ce_event" _SLEDRA_V2_CE_END_DIGEST="$_ce_digest"
                export -n _SLEDRA_V2_CE_END _SLEDRA_V2_CE_END_SIGNATURE _SLEDRA_V2_CE_END_REQUEST _SLEDRA_V2_CE_END_EVENT _SLEDRA_V2_CE_END_DIGEST
            else
                if [[ $_ce_dead == 1 ]]; then declare -g _SLEDRA_V2_STATE=disabled; export -n _SLEDRA_V2_STATE; fi
                return 0
            fi
            _sledra_ce_commit || : ;;
    esac
    if [[ $_ce_dead == 1 ]]; then
        declare -g _SLEDRA_V2_STATE=disabled
        export -n _SLEDRA_V2_STATE
    elif [[ $_ce_outcome == closed || $_ce_outcome == closing ]]; then
        declare -g _SLEDRA_V2_STATE="$_ce_outcome"
        export -n _SLEDRA_V2_STATE
    fi
    return 0
}

export -nf _sledra_ce_owner_begin _sledra_ce_end_signature _sledra_ce_owner_hook
