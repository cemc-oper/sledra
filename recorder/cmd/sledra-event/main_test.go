package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func TestEncodeValidateRetryAndConflict(t *testing.T) {
	data, err := os.ReadFile(filepath.Join("..", "..", "..", "spec", "fixtures", "1.0.0", "valid-bundle.json"))
	if err != nil {
		t.Fatal(err)
	}
	var bundle map[string]any
	if err := json.Unmarshal(data, &bundle); err != nil {
		t.Fatal(err)
	}
	event := bundle["events"].([]any)[2]
	id := event.(map[string]any)["event_id"].(string)
	eventBytes, err := json.Marshal(event)
	if err != nil {
		t.Fatal(err)
	}
	root := t.TempDir()
	input := filepath.Join(root, "input.json")
	cfg := filepath.Join(root, "config.json")
	out := filepath.Join(root, "out")
	if err := os.Mkdir(out, 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(input, eventBytes, 0600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(cfg, []byte(`{"producer_role":"process_owner"}`), 0600); err != nil {
		t.Fatal(err)
	}
	base := []string{"-config", cfg, "-input", input}
	if err := run(append([]string{"validate"}, base...)); err != nil {
		t.Fatal(err)
	}
	args := append([]string{"encode"}, append(base, "-output-dir", out)...)
	if err := run(args); err != nil {
		t.Fatal(err)
	}
	first, err := os.ReadFile(filepath.Join(out, id+".json"))
	if err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(filepath.Join(out, id+".json"))
	if err != nil || info.Mode().Perm() != 0600 {
		t.Fatalf("output mode: %v %v", info, err)
	}
	if err := run(args); err != nil {
		t.Fatal(err)
	}
	second, err := os.ReadFile(filepath.Join(out, id+".json"))
	if err != nil || !bytes.Equal(first, second) {
		t.Fatal("retry differs")
	}
	if err := os.WriteFile(filepath.Join(out, id+".json"), []byte("other"), 0600); err != nil {
		t.Fatal(err)
	}
	if err := run(args); err == nil || err.Error() != "conflicting_event_id" {
		t.Fatalf("conflict: %v", err)
	}
	if err := os.Symlink(out, filepath.Join(root, "link")); err != nil {
		t.Fatal(err)
	}
	badArgs := append([]string{"encode"}, append(base, "-output-dir", filepath.Join(root, "link"))...)
	if err := run(badArgs); err == nil || err.Error() != "output_unavailable" {
		t.Fatalf("symlink: %v", err)
	}
}
