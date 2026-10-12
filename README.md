# Sledra development workspace

This directory contains the SLD-02 **draft 1.0 contract**, its offline Go/Python consistency helpers, the SLD-03 local platform probe, the SLD-04 atomic file Registry foundation, the SLD-05 Run/Attempt context lifecycle, the SLD-06 offline event codec, the SLD-07 local session spool, and the SLD-08 thin Bash API and standalone synthetic example. Analyzer commands are not implemented yet.

The canonical portable resources are in [`spec/1.0.0/`](spec/1.0.0/). Their authoring scripts and fixtures are under [`spec/`](spec/); Go test helper is under [`recorder/`](recorder/). `sledra.local/recorder` is a local module identity for development, not a remote publication address.

From this directory, run:

```bash
python3 spec/verify.py
python3 spec/verify_event.py
python3 spec/verify_context.py
python3 tests/shell/verify.py
cd recorder && GOPROXY=off GOSUMDB=off go test ./...
```

These commands check the contract fixtures, Go/Python event bytes, the synthetic context CLI lifecycle and the disabled/enabled Shell behavior. See [`spec/README.md`](spec/README.md) for the contract, versions and limitations.

The [SLD-03 probe](tests/platform/README.md) measures local filesystem primitives, a Bash baseline, and a static build of the contract helper. Its [WSL2/ext4 result](tests/platform/results/wsl2-ext4-2026-10-11.json) does not qualify a shared or HPC filesystem.

The [SLD-04 Registry package](recorder/internal/registry/README.md) publishes immutable Run/Attempt records with their registration outbox events. The [SLD-05 context service](recorder/context/README.md) builds the public lifecycle and CLI on this foundation. Both are tested on the local ext4 development volume.

The [SLD-06 event codec](recorder/event/README.md) provides a typed Go encoding API and a standalone offline validate/encode CLI. The [SLD-07 session spool](recorder/spool/README.md) reuses those canonical bytes and supports context-aware `sledra event`. The [SLD-08 Bash API](shell/README.md) and [standalone example](examples/standalone/README.md) use explicit owner hooks; a transparent `sledra exec` supervisor is not delivered. Shared/HPC qualification remains separate work.
