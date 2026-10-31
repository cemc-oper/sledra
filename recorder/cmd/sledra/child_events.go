package main

import (
	"bytes"
	"errors"
	"io"
	"strconv"
	"strings"

	"github.com/cemc-oper/sledra/recorder/execution"
)

func childExecutionCommand(args []string, input io.Reader, output io.Writer) error {
	emit := func(reply execution.ChildReceipt) error {
		raw := reply.Bytes()
		n, err := output.Write(raw)
		if err != nil || n != len(raw) {
			return executionFailure("indeterminate", 23)
		}
		if reply.Status != 0 {
			return executionFailure(reply.Code, reply.Status)
		}
		return nil
	}
	reject := func(code string) error { return emit(execution.ChildRejection(code)) }
	budget := 0
	for _, arg := range args {
		budget += len(arg) + 1
	}
	if budget > execution.ChildArgvLimit {
		return reject("limit_exceeded")
	}
	if len(args) == 0 {
		return reject("usage")
	}
	if args[0] == "capabilities" {
		opts, _, err := parseExecutionOptions(args[1:], "--interface --protocol", false)
		if err != nil || len(opts) != 2 || opts["--interface"] != execution.ChildInterface || opts["--protocol"] != "1" {
			reply := execution.ChildRejection("protocol_mismatch")
			reply.Status = 25
			reply.Result = "incompatible"
			reply.Stop = "local"
			return emit(reply)
		}
		if !execution.ChildSupported() {
			reply := execution.ChildRejection("installation_incompatible")
			reply.Status = 25
			reply.Result = "incompatible"
			reply.Stop = "local"
			return emit(reply)
		}
		raw := []byte("CE-1\texecution-child-events\t1\n")
		n, err := output.Write(raw)
		if err != nil || n != len(raw) {
			return executionFailure("indeterminate", 23)
		}
		return nil
	}
	if args[0] == "prepare" {
		if len(args) < 2 {
			return reject("usage")
		}
		kind := args[1]
		rest := args[2:]
		allowed := ""
		typed := false
		switch kind {
		case "start":
			allowed = "--task --engine-kind --engine-service --engine-node --engine-alias"
		case "context":
			allowed = "--execution-id --task --cycle-kind --cycle --scope"
			typed = true
		case "event":
			if len(rest) == 0 {
				return reject("usage")
			}
			rest = rest[1:]
			allowed = "--execution-id"
			typed = true
		case "stage-start":
			allowed = "--execution-id --stage"
		case "stage-end":
			allowed = "--execution-id --stage --stage-id --exit-code --signal"
		case "end":
			allowed = "--execution-id --exit-code --signal"
		default:
			return reject("usage")
		}
		if _, _, err := parseExecutionOptions(rest, allowed, typed); err != nil {
			return reject(childCLIInputCode(err))
		}
		// Reuse only pure Go encoding, never the old CLI's wire/error semantics or append.
		var encoded bytes.Buffer
		if err := prepareExecution(args[1:], &encoded); err != nil {
			return reject(childCLIInputCode(err))
		}
		parts := strings.SplitN(strings.TrimSuffix(encoded.String(), "\n"), "\t", 3)
		if len(parts) != 3 {
			return reject("invalid_value")
		}
		prepared, err := execution.NewChildPrepared([]byte(parts[2]), parts[1])
		if err != nil {
			return reject(childCLIInputCode(err))
		}
		raw := prepared.Bytes()
		n, err := output.Write(raw)
		if err != nil || n != len(raw) {
			return executionFailure("indeterminate", 23)
		}
		return nil
	}
	allowed := ""
	switch args[0] {
	case "open":
		allowed = "--root --runtime-dir --owner-pid"
	case "attach", "disable":
		allowed = "--context"
	case "commit":
		allowed = "--context --role --owner-key"
	case "result":
		allowed = "--context --request-id --event-id --record-sha256"
	default:
		return reject("usage")
	}
	opts, _, err := parseExecutionOptions(args[1:], allowed, false)
	if err != nil {
		return reject(childCLIInputCode(err))
	}
	for _, key := range strings.Fields(allowed) {
		if key == "--owner-key" {
			continue
		}
		if opts[key] == "" {
			return reject("usage")
		}
	}
	recorder, err := execution.NewChildRecorder()
	if err != nil {
		reply := execution.ChildRejection("installation_incompatible")
		reply.Status = 25
		reply.Result = "incompatible"
		reply.Stop = "local"
		return emit(reply)
	}
	switch args[0] {
	case "attach":
		return emit(recorder.Attach(opts["--context"]))
	case "disable":
		return emit(recorder.Disable(opts["--context"]))
	}
	prepared, err := execution.ReadChildPrepared(input)
	if err != nil {
		return reject(childCLIInputCode(err))
	}
	switch args[0] {
	case "open":
		pid, err := strconv.Atoi(opts["--owner-pid"])
		if err != nil {
			return reject("usage")
		}
		return emit(recorder.Open(opts["--root"], opts["--runtime-dir"], pid, prepared))
	case "commit":
		return emit(recorder.Commit(opts["--context"], opts["--role"], opts["--owner-key"], prepared))
	case "result":
		if opts["--request-id"] != prepared.RequestID || opts["--event-id"] != prepared.EventID || opts["--record-sha256"] != prepared.Digest {
			return reject("invalid_value")
		}
		return emit(recorder.Lookup(opts["--context"], prepared))
	}
	return reject("usage")
}
func childCLIInputCode(err error) string {
	var e *executionError
	if errors.As(err, &e) {
		switch e.code {
		case "usage", "duplicate_key", "limit_exceeded", "reserved_event_name", "role_denied":
			return e.code
		}
	}
	switch execution.Code(err) {
	case "duplicate_key", "limit_exceeded", "reserved_event_name", "role_denied":
		return execution.Code(err)
	}
	return "invalid_value"
}
