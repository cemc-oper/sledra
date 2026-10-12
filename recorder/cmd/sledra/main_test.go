package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func writeJSON(t *testing.T, path string, v any) {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, b, 0600); err != nil {
		t.Fatal(err)
	}
}

func TestCLIContextLifecycle(t *testing.T) {
	root := t.TempDir()
	t.Setenv("SLEDRA_ROOT", root)
	t.Setenv("SLEDRA_SPEC_DIR", "../../../spec/1.0.0")
	b, err := os.ReadFile("../../../spec/fixtures/1.0.0/valid-bundle.json")
	if err != nil {
		t.Fatal(err)
	}
	var fixture map[string]any
	if err := json.Unmarshal(b, &fixture); err != nil {
		t.Fatal(err)
	}
	definition := filepath.Join(root, "definition.json")
	expectation := filepath.Join(root, "expectation.json")
	writeJSON(t, definition, fixture["definition_manifest"])
	writeJSON(t, expectation, fixture["expectation"])
	runReq := filepath.Join(root, "run-request.json")
	attemptReq := filepath.Join(root, "attempt-request.json")
	finishReq := filepath.Join(root, "finish-request.json")
	writeJSON(t, runReq, map[string]any{"registry_id": "registry-synthetic-1", "launch_token": "cli-launch", "system_id": "synthetic.model", "purpose": "test", "deployment_id": "wsl-sandbox", "platform_id": "wsl2", "cycle": map[string]any{"cycle_kind": "forecast", "cycle_time": "2026-10-09T16:00:00Z"}, "definition_file": definition, "expectation_file": expectation, "scope": []any{map[string]any{"task_key": "main.forecast", "parameters": map[string]any{"member": "01", "lead_seconds": 0}}}})
	runPath := filepath.Join(root, "contexts", "run.json")
	args := []string{"registry", "run-create", "--request-file", runReq, "--context-out", runPath}
	if err := run(args); err != nil {
		t.Fatal(err)
	}
	if err := run(args); err != nil {
		t.Fatal("retry", err)
	}
	if err := run([]string{"context", "validate", "--file", runPath}); err != nil {
		t.Fatal(err)
	}
	writeJSON(t, attemptReq, map[string]any{"task_key": "main.forecast", "parameters": map[string]any{"member": "01", "lead_seconds": 0}, "execution_token": "cli-exec", "process_role": "forecast_program"})
	attemptPath := filepath.Join(root, "contexts", "attempt.json")
	if err := run([]string{"registry", "attempt-create", "--context", runPath, "--request-file", attemptReq, "--context-out", attemptPath}); err != nil {
		t.Fatal(err)
	}
	if err := run([]string{"context", "validate", "--file", attemptPath}); err != nil {
		t.Fatal(err)
	}
	configPath := filepath.Join(root, "recorder-config.json")
	writeJSON(t, configPath, map[string]any{"producer_role": "process_owner", "producer_name": "synthetic-recorder", "producer_version": "0.1.0", "source_id": "node-a", "session_id": "process-1"})
	t.Setenv("SLEDRA_CONFIG", configPath)
	payloadPath := filepath.Join(root, "payload.json")
	writeJSON(t, payloadPath, map[string]any{"invocation_id": "synthetic-invocation", "process_role": "forecast_program"})
	var diag bytes.Buffer
	command := []string{"--context", attemptPath, "--type", "attempt.started", "--payload-file", payloadPath, "--event-id", "aa9c3e7d-0fc7-4b87-87ac-e3c9009c62d0"}
	if err := recordEvent(command, &diag); err != nil || diag.Len() != 0 {
		t.Fatalf("event: %v %s", err, diag.String())
	}
	if err := os.WriteFile(payloadPath, []byte(`{"secret":"token-value"}`), 0600); err != nil {
		t.Fatal(err)
	}
	if err := recordEvent(command, &diag); err != nil || !strings.Contains(diag.String(), `"dropped":1`) || strings.Contains(diag.String(), "token-value") {
		t.Fatalf("business: %v %s", err, diag.String())
	}
	diag.Reset()
	if err := recordEvent(append(command, "--strict"), &diag); err == nil || diag.Len() != 0 {
		t.Fatalf("strict: %v %s", err, diag.String())
	}
	var attempt map[string]any
	ab, err := os.ReadFile(attemptPath)
	if err != nil {
		t.Fatal(err)
	}
	if err := json.Unmarshal(ab, &attempt); err != nil {
		t.Fatal(err)
	}
	writeJSON(t, finishReq, map[string]any{"declaration_id": "cli-finish", "rule_version": "1", "covered_attempt_ids": []any{attempt["attempt_id"]}, "reason": "synthetic finish", "result": "finished", "source_id": "unit-test", "session_id": "cli-session"})
	if err := run([]string{"run", "finish", "--context", runPath, "--scope-file", finishReq}); err != nil {
		t.Fatal(err)
	}
	if err := run([]string{"run", "finish", "--context", runPath, "--scope-file", finishReq}); err != nil {
		t.Fatal("finish retry", err)
	}
	if err := os.Remove(runPath); err != nil {
		t.Fatal(err)
	}
	if err := run([]string{"context", "validate", "--file", runPath}); err == nil || err.Error() != "context_missing" {
		t.Fatalf("missing context: %v", err)
	}
}
