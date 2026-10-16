#!/usr/bin/env bash
set -euo pipefail

task=$1
printf '%s:%s\n' "$task" "${EXECUTION_TOKEN:?}" >>"${EXECUTION_LOG:?}"
printf '%s\n' "${SLEDRA_CONTEXT:-}" >>"${CONTEXT_PATH_LOG:?}"
mkdir -p -- "${PRODUCT_DIR:?}"
case $task in
    prepare)
        printf 'prepared\n' >"$PRODUCT_DIR/input.txt"
        printf 'prepare\n'
        ;;
    forecast)
        test -f "$PRODUCT_DIR/input.txt"
        if [[ ${FORECAST_FAIL:-0} == 1 ]]; then
            printf 'forecast failed\n' >&2
            exit 7
        fi
        printf 'synthetic forecast\n' >"$PRODUCT_DIR/forecast.txt"
        printf 'forecast\n'
        ;;
    verify)
        python3 "$(dirname -- "$0")/verify.py" "$EXPECTED_PRODUCT" \
            "$PRODUCT_DIR" "$VALIDATION_RESULT"
        ;;
    *)
        printf 'unknown task\n' >&2
        exit 2
        ;;
esac
