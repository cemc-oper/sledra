#!/usr/bin/env bash
# Synthetic owner. Keep actual business in its own, single normal invocation.
set -euo pipefail
library=${SLEDRA_BASH_LIBRARY:-${BASH_SOURCE[0]%/*}/../../integrations/bash/sledra-execution.sh}
if [[ -r $library ]]; then
    source "$library"
else
    sledra_execution_begin() { return 0; }
    sledra_execution_context() { return 0; }
    sledra_execution_end() { return 0; }
fi
finish() {
    saved=$?
    if [[ ${owner_signal:-0} != 0 ]]; then sledra_execution_end --signal "$owner_signal"
    else sledra_execution_end --exit-code "$saved"; fi
}
trap finish EXIT
trap 'owner_signal=15; exit 143' TERM
sledra_execution_begin --child-events --task synthetic.initial
sledra_execution_context --task synthetic.initial --cycle-kind none
export SLEDRA_EVENTS_LIBRARY=${SLEDRA_EVENTS_LIBRARY:-${BASH_SOURCE[0]%/*}/../../integrations/bash/sledra-events.sh}
case ${1:-external} in
    inline) (source "${BASH_SOURCE[0]%/*}/business.sh") ;;
    external) bash "${BASH_SOURCE[0]%/*}/business.sh" ;;
    *) printf 'usage: owner.sh inline|external\n' >&2; exit 2 ;;
esac
