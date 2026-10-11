# Sledra development workspace

This directory currently contains the SLD-02 **draft 1.0 contract**, its offline Go/Python consistency helpers, and the SLD-03 local platform probe. The recorder, Shell API and analyzer commands described in the plan are not implemented here yet.

The canonical portable resources are in [`spec/1.0.0/`](spec/1.0.0/). Their authoring scripts and fixtures are under [`spec/`](spec/); Go test helper is under [`recorder/`](recorder/). `sledra.local/recorder` is a local module identity for development, not a remote publication address.

From this directory, run:

```bash
python3 spec/verify.py
cd recorder && GOPROXY=off GOSUMDB=off go test ./...
```

The first command validates the same checked-in synthetic fixtures with Python and Go, checks their expected classifications, hashes and unknown-event preservation. The second runs Go golden vectors. See [`spec/README.md`](spec/README.md) for the contract, versions and limitations.

The [SLD-03 probe](tests/platform/README.md) measures local filesystem primitives, a Bash baseline, and a static build of the contract helper. Its [WSL2/ext4 result](tests/platform/results/wsl2-ext4-2026-10-11.json) does not qualify a shared or HPC filesystem.
