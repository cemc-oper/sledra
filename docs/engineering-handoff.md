# M1.5 engineering handoff

M1.5 delivers the Sledra name and standalone documentation, locked developer setup, shared build/check/regression commands, portable CLI help/version and embedded contracts, static local trial packages with isolated acceptance, parameterized source/installed tests, bounded node harnesses, CI configuration and collaboration materials. This is an engineering/local exit; actual remote CI, real HPC, release licensing and performance qualification remain separate branches in the [support matrix](support.md).

## Reusable inputs

- Run `make setup`, then `make check/test/test-platform/demo/package/test-package` as separate Make targets. These entries carry stage logs, failure summaries and protected-byte checks; CI uses the same commands.
- Keep package resources separate from runtime Registry/context/spool. Preserve the actual archive checksum, installed manifest and binary version for a trial. Source and installed test tools accept explicit binaries, specs, work parents and new report directories.
- Treat the contract as a 1.0.0 development draft. Identity derivation, canonical bytes, outbox facts, finish immutability, explicit owner hooks and business-failure isolation remain M1 invariants.
- Use the frozen [M1 handoff](../tests/m1/results/handoff.json), canonical positive/negative [fixtures](../spec/fixtures/1.0.0/) and existing replay assertions as compatibility inputs.

Frozen handoff SHA256:

```text
5c6c2bf40af052eedaf0edeb4ea02119f6d899824e086c8e26e210a436e282ef
```

The frozen bundle contains 22 raw files, two Runs, four Attempts, six contexts, eleven process events and eight outbox events. Its normal and failed/retried business statuses are 0/7/0. A late registered-only Attempt is excluded from the immutable retry Run finish scope; M2 must retain that fact rather than invent completed processing.

## M2 boundary

M2 may now begin synthetic local analysis development, with its existing unique SLD-10/11 task cards. Create the analysis Python package when that work is requested; none exists yet. First replay the unchanged frozen input, then implement source UUID/generation handling and raw-event storage/deduplication/conflict retention. SLD-12/13 later add quarantine and transactional checkpoints; real ingestion acknowledgements and the SLD-09 confirmation branch depend on SLD-13. Do not treat fixture acknowledgements as those transactions.

Routine tests do not replace the frozen handoff. A maintainer-requested refresh needs old/new hashes, identity/event review and M2 reassessment. Performance budgets, target-platform qualification and public distribution decisions limit their own branches; they do not block synthetic local M2 work.

## Outstanding branches

Actual remote Actions runs need a run link and environment evidence. Real cross-node/HPC tests need site parameters and a compute-node window. A6 requires approved budgets and target measurements. A7 requires a Sledra license and contribution/reuse/publication decision. SLD-26 later extends trial delivery to analysis products, offline upgrade/rollback and backup/recovery. True business samples, integration and M4 acceptance remain future work. Contact maintainers through [Issues](https://github.com/cemc-oper/sledra/issues).
