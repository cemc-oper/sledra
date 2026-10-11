# Sledra development workspace

This directory currently contains the SLD-02 **draft 1.0 contract** and its offline Go/Python consistency helpers. The recorder, Shell API and analyzer commands described in the plan are not implemented here yet.

The canonical portable resources are in [`spec/1.0.0/`](spec/1.0.0/). Their authoring scripts and fixtures are under [`spec/`](spec/); Go test helper is under [`recorder/`](recorder/). `sledra.local/recorder` is a local module identity for development, not a remote publication address.

From this directory, run:

```bash
python3 spec/verify.py
cd recorder && GOPROXY=off GOSUMDB=off go test ./...
```

The first command validates the same checked-in synthetic fixtures with Python and Go, checks their expected classifications, hashes and unknown-event preservation. The second runs Go golden vectors. See [`spec/README.md`](spec/README.md) for the contract, versions and limitations.
