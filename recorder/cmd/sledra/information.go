package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"

	"github.com/cemc-oper/sledra/recorder/internal/buildinfo"
)

var help = map[string]string{
	"": `Sledra records workflow identities, contexts and events; it never launches work.

Usage: sledra COMMAND [OPTIONS]
  registry run-create       Register a Run and publish its immutable context
  registry attempt-create   Register an Attempt under an explicit Run
  context validate          Validate context and immutable parent references
  run finish                Declare finish for an explicit covered Attempt set
  event                     Record a context-bound event (best effort by default)
  help [COMMAND]            Show command help
  --help, -h                Show this help without environment configuration
  --version                 Print software, build and contract provenance as JSON

Use sledra COMMAND --help for options; e.g. sledra registry run-create --help.
Recording requires an existing absolute SLEDRA_ROOT. Events also require
SLEDRA_CONFIG. The default contract is embedded 1.0.0; optional SLEDRA_SPEC_DIR
must be absolute and compatible. Successful recording leaves stdout empty.
`,
	"registry": `Usage: sledra registry run-create|attempt-create [OPTIONS]
Register identities without submitting or executing business work.
Use sledra registry run-create --help or sledra registry attempt-create --help.
`,
	"registry run-create": `Usage: sledra registry run-create --request-file FILE --context-out FILE
  --request-file FILE   JSON request with launch_token, registry_id, deployment,
                        cycle, scope, definition_file and optional expectation_file
  --context-out FILE    Immutable absolute output context path under SLEDRA_ROOT
Referenced definition/expectation files are explicit input paths. Use absolute
paths when invoking from another directory. Identical tokens reuse the winner;
changed requests conflict. No business command is executed. Stdout stays empty.
`,
	"registry attempt-create": `Usage: sledra registry attempt-create --context FILE --request-file FILE --context-out FILE
  --context FILE        Explicit immutable parent Run context
  --request-file FILE   JSON task_key, parameters, execution_token, process_role,
                        and optional retry_of
  --context-out FILE    Immutable absolute Attempt context path under SLEDRA_ROOT
Distinct execution tokens create distinct Attempts. Stdout stays empty.
`,
	"context": `Usage: sledra context validate --file FILE
Validate a context and its explicit immutable parent references.
Use sledra context validate --help for options.
`,
	"context validate": `Usage: sledra context validate --file FILE
  --file FILE           Existing Run or Attempt context with immutable parents
Requires SLEDRA_ROOT; missing/invalid parents fail without discovery or fallback.
Stdout stays empty; failures are classified JSON on stderr with exit status 1.
`,
	"run": `Usage: sledra run finish --context FILE --scope-file FILE
Declare Run finish without preventing later Attempts.
Use sledra run finish --help for options.
`,
	"run finish": `Usage: sledra run finish --context FILE --scope-file FILE
  --context FILE        Explicit immutable Run context
  --scope-file FILE     JSON declaration_id, rule_version, covered_attempt_ids,
                        reason, result, source_id and session_id
Requires SLEDRA_ROOT; appends a finish declaration. Stdout stays empty.
`,
	"event": `Usage: sledra event --context FILE --type TYPE [OPTIONS]
  --context FILE        Explicit immutable Attempt context
  --type TYPE           Event type allowed for the installed producer role
  --payload-file FILE   JSON payload; mutually exclusive with typed process flags
  --invocation-id ID    Typed attempt.started invocation identity
  --process-role ROLE   Typed attempt.started / attempt.exited process role
  --exit-code N         Typed attempt.exited exit status (0..255)
  --signal N            Typed attempt.exited signal (1..64); exclusive of exit-code
  --event-id ID         Optional event identity; default generates UUIDv4
  --strict              Return classified error and exit 1 on observation failure
Requires SLEDRA_ROOT and SLEDRA_CONFIG (producer_role, producer_name,
producer_version, source_id, session_id; optional spool limits).
Stdout stays empty. Default business mode returns 0 on observation failure and
writes one bounded JSON diagnostic with dropped:1 to stderr. No payload runs.
`,
}

// information handles only dedicated help/version forms before any environment
// or contract lookup. Business arguments retain their existing parser/protocol.
func information(args []string, output io.Writer) (bool, error) {
	if len(args) == 1 && args[0] == "--version" {
		return true, json.NewEncoder(output).Encode(buildinfo.Current())
	}
	var topic []string
	switch {
	case len(args) > 0 && args[0] == "help":
		topic = args[1:]
	case len(args) > 0 && (args[len(args)-1] == "--help" || args[len(args)-1] == "-h"):
		topic = args[:len(args)-1]
	default:
		return false, nil
	}
	text, ok := help[strings.Join(topic, " ")]
	if !ok {
		if args[0] != "help" {
			// A trailing --help may be a caller-owned flag value (e.g. a
			// filename). Let the original business parser handle it.
			return false, nil
		}
		return true, errors.New("usage")
	}
	_, err := fmt.Fprint(output, text)
	return true, err
}
