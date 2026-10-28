#!/usr/bin/env bash
# Independent P3 comparison. No generator, workflow engine or Python is used.
set -euo pipefail

if [[ $# != 1 || $1 != /* ]]; then
    printf 'usage: SLEDRA_BIN=/absolute/sledra bash compare.sh NEW_ABSOLUTE_DIRECTORY\n' >&2
    exit 2
fi
work=$1
source_path=${BASH_SOURCE[0]}
[[ $source_path == */* ]] || source_path=./$source_path
source_dir=$(cd -- "${source_path%/*}" && pwd -P)
owner=$source_dir/standalone.sh
library=${SLEDRA_BASH_LIBRARY:-$source_dir/../../integrations/bash/sledra-execution.sh}
binary=${SLEDRA_BIN:?set SLEDRA_BIN to an installed recorder}
[[ $binary == /* && -x $binary && $library == /* && -r $library ]]
# Atomic refusal to reuse any existing directory, including another trial.
mkdir -- "$work"
work=$(cd -- "$work" && pwd -P)
records=$work/records
"$binary" execution init-root --root "$records" \
    --system synthetic --deployment p3-local --partition-timezone=+00:00

cat > "$work/configure.sh" <<'BASH'
#!/usr/bin/env bash
printf 'configure\n' >> configure
if [[ $CASE == configure-failure ]]; then exit 23; fi
BASH
cat > "$work/business.sh" <<'BASH'
#!/usr/bin/env bash
printf 'run\n' >> business
printf '%s\0' "$@" >> argv
printf 'business stdout\n'
printf 'business stderr\n' >&2
case $CASE in
    failure) exit 7 ;;
    term)
        # Only this fixture's owner is signalled. It finishes waiting when this
        # child exits; this does not test arbitrary MPI signal forwarding.
        kill -TERM "$PPID"
        exit 143 ;;
esac
printf 'product bytes\n' > product
BASH
chmod u+x "$work/configure.sh" "$work/business.sh"

for scenario in success failure configure-failure term; do
    for group in off on fault; do
        destination=$work/business/$scenario/$group
        mkdir -p -- "$destination"
        mode=execution-v2
        recorder=$binary
        if [[ $group == off ]]; then mode=off; fi
        if [[ $group == fault ]]; then recorder=$work/not-installed; fi
        # A fresh Bash owns its own options/traps. Checking the subprocess's
        # status here does not put its business command in an errexit condition.
        if (
            cd -- "$destination"
            env -u SLEDRA_ENABLED \
                SLEDRA_MODE="$mode" SLEDRA_BIN="$recorder" SLEDRA_ROOT="$records" \
                SLEDRA_BASH_LIBRARY="$library" CASE="$scenario" \
                EXAMPLE_CONFIGURE="$work/configure.sh" EXAMPLE_CALLBACK_LOG="$destination/callbacks" \
                EXAMPLE_CALLBACK_EXIT_ZERO=0 EXAMPLE_CYCLE_KIND=none EXAMPLE_CYCLE= \
                bash "$owner" "$work/business.sh" 'space value' '' '中文' \
                    '$(touch must-not-run); `touch must-not-run`' $'line\nbreak'
        ) > "$destination/stdout" 2> "$destination/stderr"; then
            code=0
        else
            code=$?
        fi
        printf '%s\n' "$code" > "$destination/exit-code"
        # Missing outputs are significant and compare as empty bytes.
        for name in business argv product; do
            if [[ ! -f $destination/$name ]]; then : > "$destination/$name"; fi
        done
    done
    baseline=$work/business/$scenario/off
    case $scenario in
        success) expected=0 ;;
        failure) expected=7 ;;
        configure-failure) expected=23 ;;
        term) expected=143 ;;
    esac
    [[ $(< "$baseline/exit-code") == "$expected" ]]
    [[ $(< "$baseline/configure") == configure ]]
    if [[ $scenario == configure-failure ]]; then
        [[ ! -s $baseline/business ]]
    else
        [[ $(< "$baseline/business") == run ]]
    fi
    for group in on fault; do
        destination=$work/business/$scenario/$group
        for name in exit-code stdout business argv product configure callbacks; do
            cmp -- "$baseline/$name" "$destination/$name"
        done
        diagnostics=0
        while IFS= read -r line || [[ -n $line ]]; do
            if [[ $line == '{"ok":false,"code":"execution_observation_unavailable"}' ]]; then
                diagnostics=$((diagnostics + 1))
            else
                printf '%s\n' "$line"
            fi
        done < "$destination/stderr" > "$destination/business-stderr"
        cmp -- "$baseline/stderr" "$destination/business-stderr"
        if [[ $group == fault ]]; then
            [[ $diagnostics == 1 ]]
        else
            [[ $diagnostics == 0 ]]
        fi
        [[ ! -e $destination/must-not-run ]]
    done
done
printf 'PASS: off/on/missing-recorder; success/failure/configuration/TERM; records=%s\n' "$records"
