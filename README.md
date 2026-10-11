# Sledra development workspace

This directory currently contains the SLD-02 **draft 1.0 contract**, its offline Go/Python consistency helpers, the SLD-03 local platform probe, the SLD-04 atomic file Registry foundation, and the SLD-06 offline event codec. The production recorder CLI, Shell API and analyzer commands described in the plan are not implemented here yet.

The canonical portable resources are in [`spec/1.0.0/`](spec/1.0.0/). Their authoring scripts and fixtures are under [`spec/`](spec/); Go test helper is under [`recorder/`](recorder/). `sledra.local/recorder` is a local module identity for development, not a remote publication address.

From this directory, run:

```bash
python3 spec/verify.py
python3 spec/verify_event.py
cd recorder && GOPROXY=off GOSUMDB=off go test ./...
```

The first command validates the same checked-in synthetic fixtures with Python and Go, checks their expected classifications, hashes and unknown-event preservation. The second runs Go golden vectors. See [`spec/README.md`](spec/README.md) for the contract, versions and limitations.

The [SLD-03 probe](tests/platform/README.md) measures local filesystem primitives, a Bash baseline, and a static build of the contract helper. Its [WSL2/ext4 result](tests/platform/results/wsl2-ext4-2026-10-11.json) does not qualify a shared or HPC filesystem.

The [SLD-04 Registry package](recorder/internal/registry/README.md) publishes immutable Run/Attempt records with their registration outbox events. It is tested on the local ext4 development volume; SLD-05 will build the public context lifecycle and CLI on this foundation.

The [SLD-06 event codec](recorder/event/README.md) provides a typed Go encoding API and a standalone offline validate/encode CLI. It embeds the event schema and catalog; SLD-07 will connect its encoded bytes to a bounded spool.
