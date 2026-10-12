#!/usr/bin/env bash
set -euo pipefail

printf 'payload-start\n'
printf 'run\n' >> "${STANDALONE_EXECUTION_LOG:?}"
printf 'pipeline\n' | cat
sleep 0.01 &
wait "$!"
case ${STANDALONE_PAYLOAD_MODE:-success} in
    success)
        printf 'synthetic product\n' > "${STANDALONE_PRODUCT:?}"
        printf 'payload-end\n'
        ;;
    fail)
        printf 'payload-fail\n' >&2
        exit 7
        ;;
    hang)
        printf 'payload-ready\n'
        exec sleep 30
        ;;
    *)
        printf 'payload-mode-invalid\n' >&2
        exit 2
        ;;
esac
