#!/usr/bin/env bash
# Collect prerequisites only. Never initialize records, submit jobs, or clean sessions.
set -euo pipefail
umask 077

if [[ $# != 4 ]]; then
    printf 'usage: bash child-events-preflight.sh INSTALL SHARED_PARENT RUNTIME_DIR NEW_REPORT_DIR\n' >&2
    exit 2
fi
install=$1 shared=$2 runtime=$3 report=$4
for path in "$install" "$shared" "$runtime" "$report"; do
    if [[ $path != /* || $path == / || $path == */ || $path == *//* ||
          $path == *$'\n'* || $path == *$'\r'* || $path == *$'\t'* ||
          /${path#/}/ == */../* || /${path#/}/ == */./* ]]; then
        printf 'all paths must be absolute, normalized, non-root paths\n' >&2
        exit 2
    fi
done
for path in "$install" "$shared" "$report"; do
    if [[ $runtime/ == "$path/"* || $path/ == "$runtime/"* ]]; then
        printf 'runtime must be separate from installation, shared parent and report\n' >&2
        exit 2
    fi
done
if [[ $report/ == "$install/"* || $install/ == "$report/"* ]]; then
    printf 'report must be separate from installation\n' >&2
    exit 2
fi
mkdir -- "$report"
printf 'Report: %s/preflight.log\n' "$report"
exec > "$report/preflight.log" 2>&1
trap 'rc=$?; if [[ $rc != 0 ]]; then printf "status=failed\nexit_code=%s\na15=unverified\n" "$rc"; fi' EXIT
for tool in date uname id stat sha256sum cmp findmnt df cat; do
    command -v "$tool" >/dev/null
done
[[ $(uname -s) == Linux ]]
[[ $BASH_VERSINFO -ge 4 ]]
if [[ $BASH_VERSINFO == 4 ]]; then [[ ${BASH_VERSINFO[1]} -ge 4 ]]; fi
[[ -d $install && -d $shared && -d $runtime ]]

# Match the CE runtime's no-symlink and no-other-writers ancestry requirements.
component=$runtime
while [[ $component != / ]]; do
    [[ ! -L $component && -d $component ]]
    permissions=$(stat -c '%a' -- "$component")
    if (( (8#$permissions & 0022) != 0 )); then
        printf 'runtime ancestor permits other writers: %s\n' "$component"
        exit 1
    fi
    component=${component%/*}
    [[ -n $component ]] || component=/
done
[[ $(stat -c '%a' -- "$runtime") == 700 ]]
[[ $(stat -c '%u' -- "$runtime") == "$(id -u)" ]]
for library in sledra-execution.sh sledra-events.sh; do
    [[ -r $install/share/sledra/integrations/bash/$library ]]
done
[[ -f $install/manifest.json && -f $install/SHA256SUMS && -x $install/bin/sledra ]]
(cd "$install" && sha256sum --strict --quiet -c SHA256SUMS)
sha256sum -- "$install/manifest.json" "$install/bin/sledra" \
    "$install/share/sledra/integrations/bash/sledra-execution.sh" \
    "$install/share/sledra/integrations/bash/sledra-events.sh"
"$install/bin/sledra" --version
"$install/bin/sledra" execution child-events capabilities --interface CE-1 --protocol 1 \
    > "$report/capability.txt"
cmp -s "$report/capability.txt" <(printf 'CE-1\texecution-child-events\t1\n')

printf 'collected_utc=%s\nnode=%s\nbash=%s\nuid=%s\n' \
    "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$(uname -n)" "$BASH_VERSION" "$(id -u)"
uname -srmo
printf 'boot_id='
cat /proc/sys/kernel/random/boot_id
printf 'pid_namespace_inode='
stat -Lc '%i' /proc/self/ns/pid
printf 'collector_stat='
cat "/proc/$$/stat"
for path in "$runtime" "$shared"; do
    printf 'path=%s\n' "$path"
    stat -c 'mode=%a uid=%u gid=%g device=%d inode=%i' -- "$path"
    findmnt -T "$path" -n -o TARGET,SOURCE,FSTYPE,OPTIONS
    df -P -k -- "$path"
    df -P -i -- "$path"
done
printf 'status=preflight_passed\nstorage_semantics=unverified\nshared_visibility=unverified\na15=unverified\n'
