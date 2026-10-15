package main

import (
	"encoding/json"
	"errors"
	"flag"
	"io"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"github.com/cemc-oper/sledra/recorder/context"
	"github.com/cemc-oper/sledra/recorder/event"
	"github.com/cemc-oper/sledra/recorder/spool"
)

type eventConfig struct {
	ProducerRole    string `json:"producer_role"`
	ProducerName    string `json:"producer_name"`
	ProducerVersion string `json:"producer_version"`
	SourceID        string `json:"source_id"`
	SessionID       string `json:"session_id"`
	SegmentBytes    int64  `json:"segment_bytes"`
	SessionBytes    int64  `json:"session_bytes"`
	Segments        int    `json:"segments"`
	MaxSpoolBytes   int64  `json:"max_spool_bytes"`
	MaxSpoolFiles   int64  `json:"max_spool_files"`
}

// recordEvent binds the event to a validated Attempt context. SLD-08 adds the
// Shell wrapper; this command is already usable without it.
func recordEvent(args []string, diagnostics io.Writer) error {
	f := flag.NewFlagSet("event", flag.ContinueOnError)
	f.SetOutput(io.Discard)
	contextPath := f.String("context", "", "")
	typ := f.String("type", "", "")
	payloadPath := f.String("payload-file", "", "")
	invocationID := f.String("invocation-id", "", "")
	processRole := f.String("process-role", "", "")
	exitCode := f.String("exit-code", "", "")
	signal := f.String("signal", "", "")
	eventID := f.String("event-id", "", "")
	strict := f.Bool("strict", false, "")
	if f.Parse(args) != nil || f.NArg() != 0 || *contextPath == "" || *typ == "" {
		return errors.New("usage")
	}
	if *payloadPath != "" && (*invocationID != "" || *processRole != "" || *exitCode != "" || *signal != "") {
		return errors.New("usage")
	}
	failure := func(err error) error {
		if *strict {
			return errors.New(spool.Code(err))
		}
		_ = json.NewEncoder(diagnostics).Encode(map[string]any{"ok": false, "code": spool.Code(err), "dropped": 1})
		return nil
	}
	root := os.Getenv("SLEDRA_ROOT")
	spec := os.Getenv("SLEDRA_SPEC_DIR")
	cfgPath := os.Getenv("SLEDRA_CONFIG")
	if root == "" {
		return failure(errors.New("root_missing"))
	}
	if cfgPath == "" {
		return failure(errors.New("config_invalid"))
	}
	if spec != "" && !filepath.IsAbs(spec) {
		return failure(errors.New("contract_path_invalid"))
	}
	var cfg eventConfig
	if err := readJSON(cfgPath, &cfg); err != nil {
		return failure(errors.New("config_invalid"))
	}
	if cfg.ProducerRole == "" || cfg.ProducerName == "" || cfg.ProducerVersion == "" || cfg.SourceID == "" || cfg.SessionID == "" {
		return failure(errors.New("config_invalid"))
	}
	s, err := context.New(root, spec)
	if err != nil {
		return failure(err)
	}
	ctx, err := s.LoadContext(*contextPath)
	if err != nil {
		return failure(err)
	}
	if ctx["kind"] != "attempt_context" {
		return failure(errors.New("parent_mismatch"))
	}
	var payload []byte
	if *payloadPath != "" {
		payloadFile, err := openRegular(*payloadPath)
		if err != nil {
			return failure(errors.New("input_unavailable"))
		}
		defer payloadFile.Close()
		payload, err = event.ReadBounded(payloadFile)
		if err != nil {
			return failure(err)
		}
	} else {
		payload, err = typedPayload(*typ, *invocationID, *processRole, *exitCode, *signal)
		if err != nil {
			return failure(err)
		}
	}
	if *eventID == "" {
		*eventID, err = event.NewID()
		if err != nil {
			return failure(errors.New("spool_unavailable"))
		}
	}
	codec, err := event.New(cfg.ProducerRole)
	if err != nil {
		return failure(errors.New("contract_unavailable"))
	}
	now := time.Now().UTC().Format(time.RFC3339Nano)
	envelope := event.Envelope{SchemaVersion: "1.0.0", Kind: "event", EventID: *eventID, EventType: *typ,
		Producer:   event.Producer{Name: cfg.ProducerName, Version: cfg.ProducerVersion, Role: cfg.ProducerRole},
		Source:     event.Source{Kind: "direct", SourceID: cfg.SourceID, SessionID: cfg.SessionID},
		OccurredAt: &now, RecordedAt: now, TimeQuality: event.TimeQuality{Basis: "producer_clock"},
		Association: event.Association{RunID: ctx["run_id"].(string), TaskInstanceID: ctx["task_instance_id"].(string), AttemptID: ctx["attempt_id"].(string)}, Payload: json.RawMessage(payload)}
	encoded, err := codec.Encode(envelope)
	if err != nil {
		return failure(err)
	}
	w, err := spool.Open(root, cfg.ProducerRole, cfg.SourceID, envelope.Association.RunID, envelope.Association.TaskInstanceID, envelope.Association.AttemptID, cfg.SessionID, spool.Limits{SegmentBytes: cfg.SegmentBytes, SessionBytes: cfg.SessionBytes, Segments: cfg.Segments, MaxSpoolBytes: cfg.MaxSpoolBytes, MaxSpoolFiles: cfg.MaxSpoolFiles})
	if err != nil {
		return failure(err)
	}
	err = w.Record(encoded.Bytes, *strict)
	if closeErr := w.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return failure(err)
	}
	if stats := w.Stats(); stats.Dropped > 0 {
		return failure(errors.New(stats.LastError))
	}
	return nil
}

func typedPayload(typ, invocationID, role, exitCode, signal string) ([]byte, error) {
	switch typ {
	case "attempt.started":
		if invocationID == "" || role == "" || exitCode != "" || signal != "" {
			return nil, errors.New("schema_invalid")
		}
		return json.Marshal(map[string]any{"invocation_id": invocationID, "process_role": role})
	case "attempt.exited":
		if invocationID != "" || role == "" || (exitCode == "") == (signal == "") {
			return nil, errors.New("schema_invalid")
		}
		if exitCode != "" {
			n, err := strconv.ParseUint(exitCode, 10, 8)
			if err != nil {
				return nil, errors.New("schema_invalid")
			}
			return json.Marshal(map[string]any{"termination": "exited", "process_role": role, "exit_code": n})
		}
		n, err := strconv.ParseUint(signal, 10, 7)
		if err != nil || n == 0 || n > 64 {
			return nil, errors.New("schema_invalid")
		}
		return json.Marshal(map[string]any{"termination": "signaled", "process_role": role, "signal": n})
	default:
		return nil, errors.New("schema_invalid")
	}
}
