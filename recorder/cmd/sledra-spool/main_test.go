package main

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestAppendBusinessAndStrict(t *testing.T) {
	root := t.TempDir()
	t.Setenv("SLEDRA_ROOT", root)
	fixture, err := os.ReadFile("../../../spec/fixtures/1.0.0/valid-future-extension.json")
	if err != nil {
		t.Fatal(err)
	}
	var bundle struct {
		Events []json.RawMessage `json:"events"`
	}
	if err := json.Unmarshal(fixture, &bundle); err != nil {
		t.Fatal(err)
	}
	input := filepath.Join(t.TempDir(), "input.json")
	if err := os.WriteFile(input, bundle.Events[len(bundle.Events)-1], 0600); err != nil {
		t.Fatal(err)
	}
	config := filepath.Join(t.TempDir(), "config.json")
	if err := os.WriteFile(config, []byte(`{"producer_role":"future_role"}`), 0600); err != nil {
		t.Fatal(err)
	}
	var diag bytes.Buffer
	args := []string{"append", "--config", config, "--input", input}
	if code := run(args, &diag); code != 0 || diag.Len() != 0 {
		t.Fatalf("valid code=%d diag=%s", code, diag.String())
	}
	if code := run(args, &diag); code != 0 || diag.Len() != 0 {
		t.Fatalf("reuse code=%d diag=%s", code, diag.String())
	}
	count := 0
	err = filepath.WalkDir(filepath.Join(root, "spool"), func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if strings.HasSuffix(path, ".jsonl") {
			count++
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if count != 1 {
		t.Fatalf("segments=%d", count)
	}
	if err := os.WriteFile(input, []byte(`{"secret":"token-value"}`), 0600); err != nil {
		t.Fatal(err)
	}
	if code := run(args, &diag); code != 0 || !strings.Contains(diag.String(), `"strict":false`) || strings.Contains(diag.String(), "token-value") {
		t.Fatalf("business code=%d diag=%s", code, diag.String())
	}
	diag.Reset()
	if code := run(append(args, "--strict"), &diag); code != 1 || !strings.Contains(diag.String(), `"strict":true`) || strings.Contains(diag.String(), "token-value") {
		t.Fatalf("strict code=%d diag=%s", code, diag.String())
	}
}
