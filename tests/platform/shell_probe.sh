#!/usr/bin/env bash
set -euo pipefail

on_exit() {
    printf 'exit:%s\n' "$?" >&2
}
trap on_exit EXIT

if [[ "${1:-}" == baseline ]]; then
    printf 'start\n'
    sleep 0.01 &
    wait "$!"
    printf 'end\n'
    exit 0
fi

if [[ "${1:-}" != signal ]]; then
    printf 'usage: shell_probe.sh baseline|signal\n' >&2
    exit 2
fi

child=''
on_signal() {
    local name="$1" status="$2"
    if [[ -n "$child" ]]; then
        kill "$child" 2>/dev/null || true
        wait "$child" 2>/dev/null || true
    fi
    printf 'signal:%s\n' "$name" >&2
    exit "$status"
}
trap 'on_signal INT 130' INT
trap 'on_signal TERM 143' TERM
trap 'on_signal HUP 129' HUP

printf 'ready\n'
sleep 30 &
child=$!
wait "$child"
