# Sledra local trial installation

The Linux trial archive runs outside the source tree with the recorder binary and Bash. Python is optional for complete result verification; Go, uv, dependency caches and runtime networking are unnecessary. Installation resources and runtime data use separate directories. This is local trial delivery; public release permission and the Sledra license remain undecided.

## Build the trial

From a prepared source checkout:

```bash
make package
make test-package
```

`make package` uses the [offline developer build](development.md), with three tools in `build/package/linux-<arch>/bin/`. Outputs are `dist/sledra-<software-version>-linux-<arch>.tar.gz`, its `.tar.gz.sha256`, a detached `.manifest.json`, and `dist/latest-package.json`. The archive itself contains `manifest.json` and `SHA256SUMS`. Default software label is `dev`, separate from contract 1.0.0.

The default target is the installed Go toolchain's Linux architecture. Explicit targets and reproducible labels/times use:

```bash
GOOS=linux GOARCH=arm64 SLEDRA_VERSION=0.0.0-trial SOURCE_DATE_EPOCH=0 make package
```

Linux amd64 and arm64 are accepted build targets. Every executable must be a matching static ELF64 with neither PT_INTERP nor DT_NEEDED entries, independently of the recorded CGO_ENABLED=0 flag. Cross-builds stay unverified for runtime execution until accepted on that architecture. Native WSL2/ext4 linux/amd64 has local evidence; other platforms, real shared/HPC filesystems and software upgrades need separate qualification.

`make test-package` verifies the existing latest archive without rebuilding it. To select another artifact:

```bash
SLEDRA_PACKAGE_ARCHIVE=/absolute/path/to/sledra-dev-linux-amd64.tar.gz make test-package
```

Keep the matching `.tar.gz.sha256` next to the archive. The test rejects wrong architectures before executing them. Missing/corrupt archives fail nonzero and preserve reports. Packaging uses prepared Go caches; acceptance does not use them.

## Unpack without root

Copy the archive and its checksum to an ordinary user-owned directory. Verify the detached checksum before unpacking:

```bash
cd /path/to/downloads
sha256sum -c sledra-dev-linux-amd64.tar.gz.sha256
mkdir -p "$HOME/.local/opt"
tar -xzf sledra-dev-linux-amd64.tar.gz -C "$HOME/.local/opt"
install_dir="$HOME/.local/opt/sledra-dev-linux-amd64"
(cd "$install_dir" && sha256sum -c SHA256SUMS)
"$install_dir/bin/sledra" --version
"$install_dir/bin/sledra" --help
```

Choose a fresh installation destination for different artifacts; do not unpack over an existing installation. Checksums detect corruption, not publisher identity. The manifest records software/commit/source/build metadata, target, actual linkage, payload size/mode/hash, dependencies and local-trial/license status. Third-party materials are under `share/sledra/licenses/`; Sledra's own LICENSE is not invented.

The [package layout](../packaging/README.md) separates the recorder in `bin/` from tests in `share/sledra/test-tools/`. Only sledra is a recording entry point. Validation helpers are sledra-spec-check and sledra-event. The fixture-only retention cleaner is omitted.

## Run from another directory

The synthetic example needs Bash >=5.2 plus the ordinary dirname/cat/sleep tools. Recording itself has no Python dependency. Use a fresh writable directory outside the installation:

```bash
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/sledra-trial.XXXXXX")
mkdir "$work_dir/observability"
STANDALONE_WORK_DIR="$work_dir" SLEDRA_BIN="$install_dir/bin/sledra" \
  SLEDRA_ENABLED=1 "$install_dir/share/sledra/examples/standalone/run.sh"
printf 'Recorded files: %s\n' "$work_dir"
```

The installation may be read-only. The script locates resources relative to itself; default contracts are embedded. Use `STANDALONE_PAYLOAD_MODE=fail` with a fresh work directory for exit 7. `SLEDRA_ENABLED=0` runs the same owner/payload with inert observation. Contexts, Registry, spool and finish records are raw facts; no analysis result is produced. Direct recording configuration and errors are described by [configuration](configuration.md) and [CLI](cli.md).

## Verify installed results

Full package verification needs Python >=3.12 with its standard library only. No jsonschema, Ruff, uv or pip install is needed:

```bash
test_parent=$(mktemp -d "${TMPDIR:-/tmp}/sledra-package-test.XXXXXX")
report_dir="$test_parent/report"
python3 "$install_dir/share/sledra/test-tools/verify.py" \
  --work-root "$test_parent" --report-dir "$report_dir"
```

The verifier checks integrity, version and native architecture; compares normal/failed Shell stdout/stderr, callbacks, product and exactly-once payload logs with observation disabled; reuses M1 normal/failure/retry and frozen/fresh replay; validates canonical bundle and 19 process/outbox events with bundled Go tools. Reports, fresh handoff and commands are retained outside installation, with nonzero exit and a named cause on failure. Work directories are unique and retained for inspection; remove only directories created for your test.

Running this verifier directly does not isolate the host. Source-side `make test-package` additionally requires bubblewrap (`bwrap`), `ldd`, the system `/usr/bin/python3` >=3.12 and enabled unprivileged user/network namespaces. It mounts only the unpacked read-only package, new writable test data and selected system commands/libraries. Source, Go and dependency caches are absent; PATH is restricted and the network namespace has no route. A recording pass runs without mounting Python, then a standard-library Python pass verifies normal/failure/retry and results. Missing isolation support fails explicitly with no host fallback. Test output goes into a unique `test-results/test-package-.../` tree, preserving failure evidence.

Trial acceptance is distinct from SLD-26 full offline release/upgrade/rollback qualification and SLD-32/03 shared/HPC verification.

The trial now also includes common M1/Shell/platform/timing tools and site templates. See [HPC testing](testing-hpc.md) for shared work roots, external binaries and scoped reports. Isolated package acceptance additionally mounts grep and findmnt for the bundled regression tools; these remain test dependencies.
