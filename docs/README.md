# Sledra documentation

These guides describe the current source-based recorder and synthetic local usage. They ship inside the repository and require no neighboring workspace to read or run the examples.

| Guide | Use |
|---|---|
| [Quickstart](quickstart.md) | Prepare dependencies, build and run the synthetic recorder |
| [Architecture](architecture.md) | Understand components, identities, contexts and event flow |
| [Configuration](configuration.md) | Set paths and producer configuration |
| [Local testing](testing-local.md) | Run existing regression and frozen handoff checks |
| [Contract sources](contracts.md) | Locate contract rules and review upstream source changes |
| [Name and path migration](migration.md) | Map historical names and paths to the current project |

Detailed implementation references remain with their components: [context CLI](../recorder/context/README.md), [event codec](../recorder/event/README.md), [spool](../recorder/spool/README.md), [Registry](../recorder/internal/registry/README.md), [Bash API](../shell/README.md), [standalone example](../examples/standalone/README.md) and [fixture retention](../recorder/retention/README.md).

Installation packages, expanded CLI help, HPC harnesses and contribution/CI guides will follow in SLD-29–33. Current platform evidence is documented by the [local probe](../tests/platform/README.md); it does not establish shared/HPC support.
