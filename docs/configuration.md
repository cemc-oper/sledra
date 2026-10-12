# Sledra recording configuration

The recorder embeds the default contract and requires no source resources or installation writes. Use explicit absolute paths for direct recording inputs and runtime data. [CLI help and version](cli.md) work without environment configuration.

| Variable | Current use |
|---|---|
| `SLEDRA_ENABLED` | Bash hooks are enabled only with `1`; disabled hooks are inert |
| `SLEDRA_BIN` | Absolute executable path used by the Bash API |
| `SLEDRA_ROOT` | Existing trusted real directory holding Registry, contexts, documents, finish records and spool |
| `SLEDRA_SPEC_DIR` | Optional absolute compatible contract directory; unset/empty uses embedded resources |
| `SLEDRA_CONFIG` | Absolute JSON producer configuration path for event recording |

Producer configuration fixes `producer_role`, `producer_name`, `producer_version`, `source_id` and `session_id`; the [spool guide](../recorder/spool/README.md) describes optional segment/session and whole-tree limits. Truly concurrent producers use distinct sessions. The checked-in [process configuration](../examples/standalone/fixtures/process-config.json) and [stage configuration](../examples/standalone/fixtures/stage-config.json) are synthetic inputs, not production budgets.

Keep installation resources separate from runtime data. Run and Attempt context outputs use one immutable absolute location under the runtime root. The [context CLI](../recorder/context/README.md) describes request fields and classified errors; the [contract CLI table](../spec/README.md#cli-and-errors) lists implemented commands. Successful lifecycle commands leave stdout empty. Business-mode event errors are best effort; strict mode returns an error for validation and operator checks.

The standalone example locates its Shell library, configuration and fixtures relative to its script and uses embedded contracts by default and expects `STANDALONE_WORK_DIR` to name an existing writable synthetic test directory. Its [guide](../examples/standalone/README.md) documents failure and signal inputs. Use fresh data directories for separate manual runs.
