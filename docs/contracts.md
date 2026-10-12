# Sledra contract sources

The [contract guide](../spec/README.md), checked-in [1.0.0 resources](../spec/1.0.0/), synthetic vectors and component guides contain the versioned instructions required to use this source copy. Contract version 1.0.0 is a development draft, distinct from a software release or stage number.

## Authoritative authoring sources

The workspace authoring authority remains `observability/sledra-devel/docs/observability-plan/03-information-contracts.md` for object/event semantics and the adjacent `adr/` for design decisions. These workspace documents are not a runtime or reading dependency of an independent checkout. This repository's guides describe the implementation and do not establish a second independently evolving authority.

[contracts-sources.json](contracts-sources.json) records the source paths and SHA-256 digests reviewed for these guides. It is provenance metadata, not a second contract. To check it in the authoring workspace:

```bash
python3 scripts/check_docs.py --plan-dir /absolute/path/to/docs/observability-plan
```

A digest mismatch requires reviewing the upstream change, updating affected repository guides, schemas, generation scripts and golden vectors together where needed, then recording the reviewed source digests. The check detects source revision drift; it does not prove semantic equivalence. Run the [contract regression](testing-local.md) after semantic changes. Do not regenerate a manifest merely to bypass review.

## Machine contracts and compatibility

Edit the canonical authoring scripts under `spec/` for generated schema changes. Keep the portable resources and Go embedded event resources consistent using the existing verification commands. The [canonical identity rules](../spec/README.md#canonical-identity-bytes) and [compatibility matrix](../spec/README.md#compatibility-matrix) govern event bytes and IDs. Name/path migration changes no schema, identity or frozen handoff bytes.
