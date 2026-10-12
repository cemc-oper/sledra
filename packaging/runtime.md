# Sledra trial runtime

The recorder keeps its installation separate from runtime data. `SLEDRA_ROOT` names one existing trusted real absolute directory for Registry, contexts, documents, finish records and spools. The default 1.0.0 draft contract is embedded in the binary. Shared/HPC use requires qualification of the actual target filesystem; a successful local trial does not establish that qualification.

Source `share/sledra/shell/sledra.sh` from a known Bash owner. Set `SLEDRA_ENABLED=1` and absolute `SLEDRA_BIN`, `SLEDRA_ROOT`, `SLEDRA_CONFIG` paths. Hooks define no traps or payload launches. They preserve owner control flow and return 0; observation failure produces at most one fixed diagnostic per sourced shell. Disabled hooks are inert.

`SLEDRA_CONFIG` fixes producer_role, producer_name, producer_version, source_id and session_id, plus optional spool limits. Truly concurrent producers use different sessions. The example's process/stage configurations are synthetic test inputs, not production budgets.

Leave `SLEDRA_SPEC_DIR` unset or empty for embedded resources. A nonempty override must be an absolute directory with all nine packaged `share/sledra/spec/1.0.0` JSON resources and exactly the installed schema/catalog/capability contents. JSON formatting may differ. Relative paths report `contract_path_invalid`; missing resources report `contract_unavailable`; malformed or changed resources report `contract_incompatible`. Invalid overrides never fall back to defaults.

Discover commands with `bin/sledra --help`, `bin/sledra registry run-create --help`, `bin/sledra registry attempt-create --help`, `bin/sledra context validate --help`, `bin/sledra run finish --help` and `bin/sledra event --help`. Dedicated help/version need no runtime environment. `--version` reports software, commit, build time, source input SHA-256, Go toolchain, target and contract identity separately. Development labels are not release declarations.

Successful recording commands have empty stdout/stderr. Lifecycle/strict-event failures return 1 with classified JSON on stderr. Default business-mode event failures return 0 with a bounded diagnostic including dropped:1; stdout stays empty. Retry uncertain registration with the same token and context path, without relaunching the business command. No transparent exec supervisor, analyzer, query service or real ingestion acknowledgement is included.

Direct commands use explicit immutable parent contexts. Input paths inside requests are caller-owned; use absolute paths when invoking from another cwd. Missing parents never cause identity discovery or a guessed replacement Run.
