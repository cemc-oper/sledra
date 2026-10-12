# Sledra documentation

These guides describe the current source-based recorder and synthetic local usage. They ship inside the repository and require no neighboring workspace to read or run the examples.

| Guide | Use |
|---|---|
| [Installation](installation.md) | Build, unpack and verify the local trial without Go |
| [Quickstart](quickstart.md) | Prepare dependencies, build and run the synthetic recorder |
| [Architecture](architecture.md) | Understand components, identities, contexts and event flow |
| [Command line](cli.md) | Discover help, inspect build provenance and select contracts |
| [Configuration](configuration.md) | Set paths and producer configuration |
| [Development](development.md) | Prepare locked tools, build offline and inspect reports |
| [Local testing](testing-local.md) | Run existing regression and frozen handoff checks |
| [Contract sources](contracts.md) | Locate contract rules and review upstream source changes |
| [Name and path migration](migration.md) | Map historical names and paths to the current project |

Detailed implementation references remain with their components: [context CLI](../recorder/context/README.md), [event codec](../recorder/event/README.md), [spool](../recorder/spool/README.md), [Registry](../recorder/internal/registry/README.md), [Bash API](../shell/README.md), [standalone example](../examples/standalone/README.md) and [fixture retention](../recorder/retention/README.md).

A [local trial package](installation.md) is available; HPC harnesses and contribution/CI guides follow in SLD-32/33. Current platform evidence is documented by the [local probe](../tests/platform/README.md); it does not establish shared/HPC support.
