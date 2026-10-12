# Sledra local trial

This archive contains a Linux recorder, Bash owner hooks, synthetic examples and optional validation tools. It is a local trial, not a public release. Sledra's release license remains undecided. Third-party license files are included under `share/sledra/licenses/`; `manifest.json` identifies the build, platform and all payload files.

## Run the synthetic owner

Bash 5.2 or newer and the target binary are required for recording. The synthetic payload additionally uses the ordinary `dirname`, `cat` and `sleep` commands. No Go, Python, dependency cache, source tree or network is required to record.

After unpacking, set `install_dir` to this archive's absolute top-level directory and use a fresh writable data directory:

```bash
install_dir=/absolute/path/to/sledra-dev-linux-amd64
work_dir=$(mktemp -d "${TMPDIR:-/tmp}/sledra-trial.XXXXXX")
mkdir "$work_dir/observability"
"$install_dir/bin/sledra" --help
"$install_dir/bin/sledra" --version
STANDALONE_WORK_DIR="$work_dir" SLEDRA_BIN="$install_dir/bin/sledra" \
  SLEDRA_ENABLED=1 "$install_dir/share/sledra/examples/standalone/run.sh"
printf 'Recorded files: %s\n' "$work_dir"
```

The script works from any cwd and locates its library, configuration and inputs relative to itself. Installation files may be read-only; data always belongs in the explicit work directory. Use `STANDALONE_PAYLOAD_MODE=fail` in a separate fresh directory to return 7. `SLEDRA_ENABLED=0` runs the same payload with observation disabled. Raw context/Registry/spool/finish files are facts, not analysis results.

## Verify the trial

Checksums detect accidental changes; they are not a signature or proof of publisher identity. From the archive's top-level directory:

```bash
sha256sum -c SHA256SUMS
```

Optional complete verification requires Python 3.12 or newer with only its standard library; no pip/uv packages are needed. Set existing writable `test_parent` and a new `report_dir` outside the installation:

```bash
python3 "$install_dir/share/sledra/test-tools/verify.py" \
  --work-root "$test_parent" --report-dir "$report_dir"
```

This checks the manifest, executable architecture/linkage, binary version, normal/failed Shell payload equivalence, M1 normal/failure/retry, frozen/fresh handoff replay, canonical bundle and 19 recorded/outbox events. It returns nonzero and preserves a summary plus commands on failure. Test data is created in a unique directory under the supplied parent and retained for inspection. It does not itself isolate the host network; source-side `make test-package` supplies that isolation and proves source/Go/cache absence.

Only `bin/sledra` is a recording entry point. The helpers under `share/sledra/test-tools/bin` validate synthetic inputs; they are not additional production services. The fixture-only retention cleaner is omitted. Architecture labels describe executable format; consult the separate acceptance report for actual execution evidence. A cross-compiled archive is unverified until run on its named platform. Target shared/HPC filesystem qualification remains separate.

See `share/sledra/docs/runtime.md` for paths and output behavior.
