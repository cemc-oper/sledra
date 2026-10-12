# Local trial packaging

`make package` builds a local Linux trial from the current source using the shared build recipe, then emits a tar.gz, detached manifest and archive checksum in `dist/`. `make test-package` checks the existing artifact; it does not rebuild a broken or missing package. See [installation](../docs/installation.md) for use and verification.

```text
sledra-<software-label>-linux-<arch>/
├── bin/sledra
├── README.md
├── manifest.json
├── SHA256SUMS
└── share/sledra/
    ├── shell/
    ├── spec/{1.0.0,fixtures/1.0.0}/
    ├── examples/standalone/
    ├── docs/runtime.md
    ├── licenses/
    ├── scripts/test_tools.py
    ├── tests/m1/{verify.py,replay.py,results/handoff.json}
    └── test-tools/{verify.py,package_tools.py,bin/}
```

The installed Python verifier reuses M1 assertions with an explicit precompiled recorder path. Test helpers are sledra-spec-check and sledra-event; sledra-retention and sledra-spool are omitted. No Go source, module cache, virtual environment, analysis package or Python third-party library ships.

The build defaults to software label dev, supports linux/amd64 and linux/arm64, and records actual ELF headers/linkage. All binaries must have no PT_INTERP or DT_NEEDED entries. CGO_ENABLED=0 is a build setting, separately checked against actual ELF bytes. A package manifest leaves runtime validation unverified; the acceptance report records an actual native run. Cross-compilation alone never qualifies a target.

Payload file size/mode/SHA-256 entries are in manifest.json. SHA256SUMS also hashes the manifest; the detached .tar.gz.sha256 hashes the archive. Archive ownership and timestamps are normalized. With unchanged inputs, cached build provenance and package bytes repeat; SOURCE_DATE_EPOCH supplies reproducible embedded build time. Content changing under the same software label replaces that local ignored artifact deliberately. This is not a signed public distribution.

Third-party license files come from the modules actually linked into these binaries and the installed Go toolchain. Sledra's own release license remains undecided; the local trial manifest records that status. No old reference implementation is copied. Public release, upgrade/rollback qualification and real HPC deployment remain later work.
