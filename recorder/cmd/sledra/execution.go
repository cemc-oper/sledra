package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"slices"
	"strings"

	"github.com/cemc-oper/sledra/recorder/execution"
)

// This branch uses only the independent Execution core and standard library.
// Errors contain fixed codes, never command arguments or filesystem paths.
type executionError struct {
	code   string
	status int
}

func (e *executionError) Error() string              { return e.code }
func executionFailure(code string, status int) error { return &executionError{code, status} }

func executionProblem(err error, status int) error {
	return executionFailure(execution.Code(err), status)
}

// Reject the marker before any legacy service or event producer is constructed.
// Lstat also rejects dangling marker links and unexpected marker file types.
func rejectExecutionRoot(root string) error {
	if root == "" {
		return nil
	}
	_, err := os.Lstat(filepath.Join(root, "sledra.json"))
	if !errors.Is(err, os.ErrNotExist) {
		return errors.New("root_incompatible")
	}
	return nil
}

type executionOptions map[string]string

// Fixed arities preserve negative values and avoid last-wins flag parsing.
// Typed values are deliberately limited to context parameters in P1-02.
func parseExecutionOptions(args []string, allowed string, typed bool) (executionOptions, map[string]any, error) {
	opts, values := executionOptions{}, map[string]any{}
	for len(args) > 0 {
		arg := args[0]
		args = args[1:]
		if typed && (arg == "--string" || arg == "--int" || arg == "--float" || arg == "--bool" || arg == "--null") {
			arity := 2
			if arg == "--null" {
				arity = 1
			}
			if len(args) < arity {
				return nil, nil, executionFailure("usage", 2)
			}
			key := args[0]
			if _, exists := values[key]; exists {
				return nil, nil, executionFailure("duplicate_key", 2)
			}
			var value any
			if arity == 2 {
				raw := args[1]
				if len(raw) > execution.MaxTextBytes {
					return nil, nil, executionFailure("limit_exceeded", 2)
				}
				if arg == "--string" {
					value = raw
				} else {
					decoded, err := execution.Decode([]byte(raw))
					if err != nil {
						return nil, nil, executionProblem(err, 2)
					}
					switch arg {
					case "--bool":
						if raw != "true" && raw != "false" {
							return nil, nil, executionFailure("schema_invalid", 2)
						}
					case "--int", "--float":
						// Require a numeric token, preserving its exact original lexeme.
						number, numeric := decoded.(json.Number)
						if !numeric || string(number) != raw ||
							(strings.ContainsAny(raw, ".eE") != (arg == "--float")) {
							return nil, nil, executionFailure("invalid_number", 2)
						}
					}
					value = decoded
				}
			}
			values[key] = value
			args = args[arity:]
			continue
		}
		name, value, equal := strings.Cut(arg, "=")
		if !slices.Contains(strings.Fields(allowed), name) {
			return nil, nil, executionFailure("usage", 2)
		}
		if _, exists := opts[name]; exists {
			return nil, nil, executionFailure("usage", 2)
		}
		if name == "--create" {
			if equal {
				return nil, nil, executionFailure("usage", 2)
			}
			value = "true"
		} else if !equal {
			if len(args) == 0 {
				return nil, nil, executionFailure("usage", 2)
			}
			value, args = args[0], args[1:]
		}
		opts[name] = value
	}
	return opts, values, nil
}

func optionalExecutionString(opts executionOptions, key string) any {
	if value, exists := opts[key]; exists {
		return value
	}
	return nil
}

func executionCommand(args []string, input io.Reader, output io.Writer) error {
	if len(args) == 0 {
		return executionFailure("usage", 2)
	}
	switch args[0] {
	case "init-root":
		opts, _, err := parseExecutionOptions(args[1:], "--root --system --deployment --partition-timezone", false)
		if err != nil {
			return err
		}
		if len(opts) != 4 || !filepath.IsAbs(opts["--root"]) {
			return executionFailure("usage", 2)
		}
		config := execution.RootConfig{FormatVersion: execution.FormatVersion, LayoutVersion: execution.LayoutVersion,
			Namespace: execution.Namespace{System: opts["--system"], Deployment: opts["--deployment"]}, PartitionTimezone: opts["--partition-timezone"]}
		// Validate deployment input before allowing InitRoot to touch the filesystem.
		codec, err := execution.NewCodec()
		if err != nil {
			return executionProblem(err, 3)
		}
		_, err = codec.Encode("root", map[string]any{"format_version": config.FormatVersion, "layout_version": config.LayoutVersion,
			"namespace": map[string]any{"system": config.Namespace.System, "deployment": config.Namespace.Deployment}, "partition_timezone": config.PartitionTimezone})
		if err != nil {
			return executionProblem(err, 2)
		}
		if err := execution.InitRoot(opts["--root"], config); err != nil {
			return executionProblem(err, 3)
		}
		return nil
	case "prepare":
		return prepareExecution(args[1:], output)
	case "append":
		return appendExecution(args[1:], input)
	default:
		return executionFailure("usage", 2)
	}
}

func prepareExecution(args []string, output io.Writer) error {
	if len(args) == 0 {
		return executionFailure("usage", 2)
	}
	kind := args[0]
	var allowed string
	switch kind {
	case "start":
		allowed = "--task --engine-kind --engine-service --engine-node --engine-alias"
	case "context":
		allowed = "--execution-id --task --cycle-kind --cycle --scope --previous-event"
	case "end":
		allowed = "--execution-id --exit-code --signal --previous-event"
	default:
		return executionFailure("usage", 2)
	}
	opts, params, err := parseExecutionOptions(args[1:], allowed, kind == "context")
	if err != nil {
		return err
	}
	codec, err := execution.NewCodec()
	if err != nil {
		return executionProblem(err, 3)
	}
	var raw []byte
	aux := "-"
	if kind == "start" {
		var task *string
		if value, exists := opts["--task"]; exists {
			task = &value
		}
		var engine map[string]any
		if len(opts) > 0 && (len(opts) != 1 || task == nil) {
			engine = map[string]any{"kind": opts["--engine-kind"], "service": optionalExecutionString(opts, "--engine-service"), "node": optionalExecutionString(opts, "--engine-node")}
			if alias, exists := opts["--engine-alias"]; exists {
				if alias != "true" && alias != "false" {
					return executionFailure("schema_invalid", 2)
				}
				engine["alias"] = alias == "true"
			}
		}
		raw, err = codec.NewStart(task, engine)
	} else {
		name := "execution.context"
		attrs := map[string]any{"task": opts["--task"], "parameters": params, "cycle_kind": opts["--cycle-kind"],
			"cycle": optionalExecutionString(opts, "--cycle"), "scope": optionalExecutionString(opts, "--scope"), "run_id": nil}
		if kind == "end" {
			name, attrs = "execution.ended", map[string]any{}
			for _, key := range []string{"--exit-code", "--signal"} {
				if value, exists := opts[key]; exists {
					decoded, decodeErr := execution.Decode([]byte(value))
					if decodeErr != nil || value != strings.TrimSpace(value) {
						return executionFailure("schema_invalid", 2)
					}
					attrs[strings.ReplaceAll(strings.TrimPrefix(key, "--"), "-", "_")] = decoded
				}
			}
		}
		if previous, exists := opts["--previous-event"]; exists {
			prior, validateErr := codec.Validate("event", []byte(previous))
			if validateErr != nil {
				return executionProblem(validateErr, 2)
			}
			if prior["execution_id"] != opts["--execution-id"] || prior["name"] != name {
				return executionFailure("execution_mismatch", 2)
			}
			candidate := make(map[string]any, len(prior))
			for key, value := range prior {
				candidate[key] = value
			}
			candidate["attributes"] = attrs
			encoded, encodeErr := codec.Encode("event", candidate)
			if encodeErr != nil {
				return executionProblem(encodeErr, 2)
			}
			validated, _ := codec.Validate("event", encoded)
			if !execution.Equivalent("event", prior, validated) {
				return executionFailure("conflicting_"+kind, 3)
			}
			raw = []byte(previous)
		} else {
			raw, err = codec.NewEvent(opts["--execution-id"], name, attrs)
		}
	}
	if err != nil {
		status := 2
		if execution.Code(err) == "identity_unavailable" {
			status = 3
		}
		return executionProblem(err, status)
	}
	event, err := codec.Validate("event", raw)
	if err != nil {
		return executionProblem(err, 2)
	}
	if kind == "start" {
		aux = event["occurred_at"].(string)
	}
	receipt := fmt.Sprintf("%s\t%s\t%s\n", event["execution_id"], aux, raw)
	if len(receipt) > 65792 || bytes.ContainsAny(raw, "\t\r\n") {
		return executionFailure("limit_exceeded", 2)
	}
	n, err := io.WriteString(output, receipt)
	if err != nil || n != len(receipt) {
		return executionFailure("indeterminate", 3)
	}
	return nil
}

func appendExecution(args []string, input io.Reader) error {
	opts, _, err := parseExecutionOptions(args, "--root --execution-id --entry-time --create", false)
	if err != nil {
		return err
	}
	if !filepath.IsAbs(opts["--root"]) {
		return executionFailure("usage", 2)
	}
	entry, err := execution.ParseTime(opts["--entry-time"])
	if err != nil {
		return executionProblem(err, 2)
	}
	// The record bound excludes LF. Read one extra byte to reject excess data.
	line, err := io.ReadAll(io.LimitReader(input, execution.MaxRecordBytes+2))
	if err != nil {
		return executionFailure("input_unavailable", 3)
	}
	if len(line) > execution.MaxRecordBytes+1 {
		return executionFailure("limit_exceeded", 2)
	}
	if len(line) == 0 || line[len(line)-1] != '\n' {
		return executionFailure("invalid_json", 2)
	}
	raw := line[:len(line)-1]
	codec, err := execution.NewCodec()
	if err != nil {
		return executionProblem(err, 3)
	}
	event, err := codec.Validate("event", raw)
	if err != nil {
		return executionProblem(err, 2)
	}
	if event["execution_id"] != opts["--execution-id"] {
		return executionFailure("execution_mismatch", 2)
	}
	create := opts["--create"] == "true"
	if create != (event["name"] == "execution.started") {
		return executionFailure("schema_invalid", 2)
	}
	if create {
		started, _ := execution.ParseTime(event["occurred_at"].(string))
		if !entry.Equal(started) {
			return executionFailure("entry_mismatch", 2)
		}
	}
	root, err := execution.OpenRoot(opts["--root"])
	if err != nil {
		return executionProblem(err, 3)
	}
	defer root.Close()
	var writer *execution.Writer
	if create {
		writer, err = root.Create(raw)
	} else {
		writer, err = root.OpenWriter(opts["--execution-id"], opts["--entry-time"])
	}
	if err != nil {
		return executionProblem(err, 3)
	}
	if !create {
		err = writer.Append(raw)
	}
	closeErr := writer.Close()
	if err == nil {
		err = closeErr
	}
	if err != nil {
		return executionProblem(err, 3)
	}
	return nil
}
