package context

import (
	"bytes"
	"path/filepath"
	"regexp"
	"sort"
	"time"

	"sledra.local/recorder/internal/speccontract"
)

type FinishRequest struct {
	Run               map[string]any `json:"-"`
	DeclarationID     string         `json:"declaration_id"`
	RuleVersion       string         `json:"rule_version"`
	CoveredAttemptIDs []string       `json:"covered_attempt_ids"`
	Reason            string         `json:"reason"`
	Result            string         `json:"result"`
	SourceID          string         `json:"source_id"`
	SessionID         string         `json:"session_id"`
}
type FinishResult struct {
	Path    string
	Event   map[string]any
	Created bool
}

var attemptID = regexp.MustCompile(`^att_[0-9a-f]{64}$`)

// Finish declares an immutable scope snapshot. It never closes a Run or
// prevents later Attempt registration. Concurrent declarations coexist.
func (s *Service) Finish(req FinishRequest) (*FinishResult, error) {
	if req.DeclarationID == "" || req.RuleVersion == "" || req.Reason == "" || req.SourceID == "" || req.SessionID == "" {
		return nil, fail("schema_invalid")
	}
	if req.Result != "finished" && req.Result != "abandoned" && req.Result != "unknown" {
		return nil, fail("schema_invalid")
	}
	run, err := s.ValidateRun(req.Run)
	if err != nil {
		return nil, err
	}
	ids := append([]string(nil), req.CoveredAttemptIDs...)
	sort.Strings(ids)
	for i, id := range ids {
		if !attemptID.MatchString(id) || (i > 0 && ids[i-1] == id) {
			return nil, fail("semantic_invalid")
		}
		record, err := s.registry.Read(str(run["registry_id"]), "attempt", id)
		if err != nil {
			return nil, fail("parent_mismatch")
		}
		attempt := obj(record.Record["context"])
		if str(attempt["run_id"]) != str(run["run_id"]) || str(attempt["run_context_sha256"]) != str(run["context_sha256"]) {
			return nil, fail("parent_mismatch")
		}
		if _, err := s.ValidateAttempt(attempt); err != nil {
			return nil, fail("parent_mismatch")
		}
	}
	values := make([]any, len(ids))
	for i, id := range ids {
		values[i] = id
	}
	setSHA, err := speccontract.SHA(values)
	if err != nil {
		return nil, fail("semantic_invalid")
	}
	scopeSHA, err := speccontract.SHA(run["scope"])
	if err != nil {
		return nil, fail("semantic_invalid")
	}
	scopeRef := "scope_" + scopeSHA
	request := map[string]any{"run_id": run["run_id"], "run_context_sha256": run["context_sha256"], "declaration_id": req.DeclarationID, "scope_ref": scopeRef, "rule_version": req.RuleVersion, "covered_attempt_ids": values, "covered_attempts_sha256": setSHA, "reason": req.Reason, "result": req.Result, "source_id": req.SourceID, "session_id": req.SessionID}
	requestSHA, err := speccontract.SHA(request)
	if err != nil {
		return nil, fail("semantic_invalid")
	}
	path := filepath.Join(s.root, "finish", shaString(str(run["registry_id"])), str(run["run_id"]), shaString(req.DeclarationID)+".json")
	if err := realPath(s.root, path, true); err != nil {
		return nil, err
	}
	if b, err := readBounded(path); err == nil {
		return s.finishWinner(path, b, requestSHA, run)
	} else if !isCode(err, "context_missing") {
		return nil, err
	}
	at := time.Now().UTC().Format(time.RFC3339Nano)
	eventID := "evt_" + speccontract.LengthDigest("finish", str(run["run_id"]), req.DeclarationID)
	event := map[string]any{"schema_version": "1.0.0", "kind": "event", "event_id": eventID, "event_type": "run.finish_declared", "producer": map[string]any{"name": "sledra-recorder", "version": "0.1.0", "role": "lifecycle_owner"}, "source": map[string]any{"kind": "direct", "source_id": req.SourceID, "session_id": req.SessionID}, "occurred_at": at, "recorded_at": at, "time_quality": map[string]any{"basis": "producer_clock"}, "association": map[string]any{"run_id": run["run_id"]}, "payload": map[string]any{"declaration_id": req.DeclarationID, "scope_ref": scopeRef, "rule_version": req.RuleVersion, "covered_attempt_ids": values, "covered_attempts_sha256": setSHA, "reason": req.Reason, "result": req.Result}}
	eventBytes, err := speccontract.Canonical(event)
	if err != nil {
		return nil, fail("schema_invalid")
	}
	if _, err := s.finishCodec.Validate(eventBytes); err != nil {
		return nil, fail("schema_invalid")
	}
	record := map[string]any{"schema_version": "1.0.0", "record_kind": "finish_record", "registry_id": run["registry_id"], "run_id": run["run_id"], "declaration_id": req.DeclarationID, "request_sha256": requestSHA, "outbox_event": event}
	record["record_sha256"], err = speccontract.ContentSHA(record, "record_sha256")
	if err != nil {
		return nil, fail("semantic_invalid")
	}
	data, err := speccontract.Canonical(record)
	if err != nil {
		return nil, fail("schema_invalid")
	}
	winner, created, err := publish(s.root, path, data)
	if err != nil {
		return nil, err
	}
	result, err := s.finishWinner(path, winner, requestSHA, run)
	if err != nil {
		return nil, err
	}
	result.Created = created
	return result, nil
}

func (s *Service) finishWinner(path string, data []byte, requestSHA string, run map[string]any) (*FinishResult, error) {
	v, err := speccontract.Decode(data)
	if err != nil {
		return nil, fail("context_invalid")
	}
	record, ok := v.(map[string]any)
	if !ok || len(record) != 8 {
		return nil, fail("context_invalid")
	}
	for _, key := range []string{"schema_version", "record_kind", "registry_id", "run_id", "declaration_id", "request_sha256", "outbox_event", "record_sha256"} {
		if _, ok := record[key]; !ok {
			return nil, fail("context_invalid")
		}
	}
	digest, err := speccontract.ContentSHA(record, "record_sha256")
	if err != nil || str(record["record_sha256"]) != digest || str(record["schema_version"]) != "1.0.0" || str(record["record_kind"]) != "finish_record" || str(record["registry_id"]) != str(run["registry_id"]) || str(record["run_id"]) != str(run["run_id"]) {
		return nil, fail("context_invalid")
	}
	event := obj(record["outbox_event"])
	if event == nil {
		return nil, fail("context_invalid")
	}
	b, err := speccontract.Canonical(event)
	if err != nil {
		return nil, fail("context_invalid")
	}
	validated, err := s.finishCodec.Validate(b)
	if err != nil || !bytes.Equal(bytes.TrimSpace(validated.Bytes), b) || str(event["event_type"]) != "run.finish_declared" || str(obj(event["association"])["run_id"]) != str(run["run_id"]) || str(obj(event["payload"])["declaration_id"]) != str(record["declaration_id"]) || str(event["event_id"]) != "evt_"+speccontract.LengthDigest("finish", str(run["run_id"]), str(record["declaration_id"])) {
		return nil, fail("context_invalid")
	}
	if str(record["request_sha256"]) != requestSHA {
		return nil, fail("conflict")
	}
	scopeSHA, err := speccontract.SHA(run["scope"])
	if err != nil || str(obj(event["payload"])["scope_ref"]) != "scope_"+scopeSHA {
		return nil, fail("context_invalid")
	}
	return &FinishResult{Path: path, Event: event}, nil
}
