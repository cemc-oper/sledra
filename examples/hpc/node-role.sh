#!/usr/bin/env bash
set -euo pipefail
if (( BASH_VERSINFO[0] < 4 || (BASH_VERSINFO[0] == 4 && BASH_VERSINFO[1] < 4) )); then
    printf 'Sledra HPC worker requires Bash >=4.4; got %s\n' "$BASH_VERSION" >&2
    exit 2
fi
printf 'Sledra HPC worker on %s: Bash %s\n' "$(hostname)" "$BASH_VERSION"
# srun starts exactly one task on each of two compute nodes.
: "${SLURM_PROCID:?run through the generated two-node srun job}"
case "$SLURM_PROCID" in
    0) role=a ;;
    1) role=b ;;
    *) printf 'Unexpected role index: %s\n' "$SLURM_PROCID" >&2; exit 2 ;;
esac
if [[ $# != 3 ]]; then
    printf 'Usage: node-role.sh PYTHON TEST_HOME ABSOLUTE_RUN_DIR\n' >&2
    exit 2
fi
export PYTHONDONTWRITEBYTECODE=1
exec "$1" "$2/tests/platform/cross_node.py" worker --run-dir "$3" --role "$role"
