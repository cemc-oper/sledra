# Sledra name and path migration

SLD-28 moved the development workspace and its embedded Git repository on 2026-10-12. Product, repository and CLI names use `sledra`; environment variables retain `SLEDRA_*`.

| Historical location or identity | Current location or identity |
|---|---|
| `observability/sladra-devel/` | `observability/sledra-devel/` |
| `observability/sladra-devel/repo/sladra/` | `observability/sledra-devel/repo/sledra/` |
| `sladra-devel/docs/observability-plan/` | `sledra-devel/docs/observability-plan/` |
| `sledra.local/recorder` | `github.com/cemc-oper/sledra/recorder` |

The workspace prefix is `/home/wangdp/project/mcv/mcv-workflow-project/mcv-workflow-devel/`. Windows users replace `sladra-devel` and `repo\sladra` in the corresponding `\\wsl.localhost\Ubuntu-24.04\home\wangdp\project\mcv\mcv-workflow-project\mcv-workflow-devel\observability\` paths. Update editor roots and external bookmarks to the new paths. No compatibility symlink is installed.

## Migration inventory

Before moving, the repository was clean at `f6d527c681b8367123a1dd3e608c4ef62ee032ac` on `main`. It had one ordinary `.git/` directory, one worktree, no submodules and no relevant symlinks. The outer workspace was not a Git repository. No other project's active files referenced the old project name or paths. Origin was already `git@github.com:cemc-oper/sledra.git`; migration did not change it or contact the remote.

Both directories were renamed on the same filesystem. All 152 existing workspace files matched their pre-move SHA-256 values before editing; HEAD and worktree status also matched. The migration preserves Git history and any pre-existing local files. Go module identity and imports now use the origin-derived path. Repository docs provide standalone navigation; external plan links target the new paths.

## Historical evidence

Historical workspace evidence retains original path text, commands, commit IDs and digests. Its navigation links point to current files, and each affected historical document explains the retained names. Historical JSON reports retain original filesystem/source paths byte for byte. They describe the original measurement environment, not a current executable entry point.

The frozen [M1 handoff](../tests/m1/results/handoff.json) remains at SHA-256 `5c6c2bf40af052eedaf0edeb4ea02119f6d899824e086c8e26e210a436e282ef`. All existing schemas, fixtures and historical JSON reports retain their bytes. The portable [M1 replay](../tests/m1/README.md) remains the compatibility input for analysis work.

To undo the physical migration, first leave both directories, move `repo/sledra` back to `repo/sladra`, then move `observability/sledra-devel` back to `observability/sladra-devel`. This preserves the current working edits; reverting module/docs changes is a separate Git operation. Always check that the destination does not already exist.
