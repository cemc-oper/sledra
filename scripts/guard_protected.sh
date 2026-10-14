#!/usr/bin/env bash
# Preserve frozen contracts and fixtures across public development commands.
set -euo pipefail

snapshot() {
    find spec/1.0.0 spec/fixtures recorder/event/contract \
        recorder/internal/speccontract/contract examples/standalone/fixtures \
        tests/m1/results tests/platform/results -type f -print0 \
        | sort -z | xargs -0 sha256sum
}

before=$(snapshot)
status=0
"$@" || status=$?
after=$(snapshot)
if [[ $before != "$after" ]]; then
    printf '%s\n' 'Protected fixture bytes changed:' >&2
    diff -u <(printf '%s\n' "$before") <(printf '%s\n' "$after") >&2 || :
    exit 1
fi
exit "$status"
