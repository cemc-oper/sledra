# Sledra recording configuration

Use explicit absolute paths for direct recorder calls. The current source build has no embedded default context contract; its default `../spec/1.0.0` depends on the working directory. Portable resource lookup belongs to SLD-30.

| Variable | Current use |
|---|---|
| `SLEDRA_ENABLED` | Bash hooks are enabled only with `1`; disabled hooks are inert |
| `SLEDRA_BIN` | Absolute executable path used by the Bash API |
| `SLEDRA_ROOT` | Existing trusted real directory holding Registry, contexts, documents, finish records and spool |
| `SLEDRA_SPEC_DIR` | Absolute path to the checked-in `spec/1.0.0` resources |
| `SLEDRA_CONFIG` | Absolute JSON producer configuration path for event recording |

Producer configuration fixes `producer_role`, `producer_name`, `producer_version`, `source_id` and `session_id`; the [spool guide](../recorder/spool/README.md) describes optional segment/session and whole-tree limits. Truly concurrent producers use distinct sessions. The checked-in [process configuration](../examples/standalone/fixtures/process-config.json) and [stage configuration](../examples/standalone/fixtures/stage-config.json) are synthetic inputs, not production budgets.

Keep installation resources separate from runtime data. Run and Attempt context outputs use one immutable absolute location under the runtime root. The [context CLI](../recorder/context/README.md) describes request fields and classified errors; the [contract CLI table](../spec/README.md#cli-and-errors) lists implemented commands. Successful lifecycle commands leave stdout empty. Business-mode event errors are best effort; strict mode returns an error for validation and operator checks.

The standalone example supplies resource defaults relative to its script and expects `STANDALONE_WORK_DIR` to name an existing writable synthetic test directory. Its [guide](../examples/standalone/README.md) documents failure and signal inputs. Use fresh data directories for separate manual runs.
