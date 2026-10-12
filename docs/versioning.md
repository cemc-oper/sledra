# Software and contract versions

Recorder software versions, contract versions and engineering-plan revisions are independent. The current software label is `dev`; `spec/1.0.0` is a development contract draft. Root Python metadata `0.0.0` identifies development tooling and is not a released recorder or future analysis library. No stable release, public tag or release license has been approved.

## Software label policy

Maintainer-approved numbered software versions use SemVer: `0.MINOR.PATCH` while the public API is still evolving, with prerelease suffixes such as `0.1.0-trial.1` for trials. Before 1.0, an intentional incompatible software interface change increments MINOR; compatible fixes increment PATCH. A later 1.0 release needs an explicit interface/support/release decision. These examples are policy, not existing releases.

Set `SLEDRA_VERSION` deliberately during a build. Labels accept letters, digits, dots, plus and hyphens; a numbered release must also follow the policy above. The builder is not a release manager and does not enforce SemVer or create tags. `dev` artifacts may be replaced locally as inputs change; approved numbered distribution labels must identify immutable content, with new labels for changed binaries/resources. Future Git tags would use `v<software-version>` only after review and publication approval. CI currently tests branch/PR inputs and creates no tags or releases.

`SOURCE_DATE_EPOCH` can set the embedded UTC build timestamp. `sledra --version` reports software label, commit (including dirty/unknown), recorder source digest, build time, Go version, OS/architecture and contract version. Unknown metadata from direct source builds must remain unknown rather than fabricated. Build/package manifests and archive checksums identify exact content; they do not prove publisher identity.

## Compatibility and frozen input

A software version change does not authorize an event/schema/identity change. Contract changes require the canonical-spec review and compatibility vectors described by [contract sources](contracts.md), updates to embedded copies and explicit reader/writer implications. A contract draft number is not a guarantee that every future 1.x behavior is already implemented.

M1.5 r8 is an engineering-plan revision, not a software release or contract bump. M2 starts by replaying the existing M1 handoff and retaining its digest. Deliberate fixture refresh requires explicit review, old/new hashes and M2 input reassessment; see the [engineering handoff](engineering-handoff.md). A routine test run never updates frozen evidence.
