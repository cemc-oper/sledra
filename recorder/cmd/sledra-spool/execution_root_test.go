package main

import (
	"bytes"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestLegacySpoolRejectsExecutionRootBeforeLoadingInputs(t *testing.T) {
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "sledra.json"), []byte(`{"format_version":"2.0.0"}`), 0600); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SLEDRA_ROOT", root)
	for _, strict := range []bool{false, true} {
		var diagnostics bytes.Buffer
		args := []string{"append", "--config", "/must-not-load", "--input", "/must-not-load"}
		want := 0
		if strict {
			args = append(args, "--strict")
			want = 1
		}
		if code := run(args, &diagnostics); code != want || diagnostics.Len() == 0 || diagnostics.Len() > 256 {
			t.Fatal(code, diagnostics.String())
		}
		if !strings.Contains(diagnostics.String(), `"code":"spool_unavailable"`) {
			t.Fatal("root refusal did not precede input loading", diagnostics.String())
		}
	}
	entries, err := os.ReadDir(root)
	if err != nil || len(entries) != 1 || entries[0].Name() != "sledra.json" {
		t.Fatal("legacy root side effects", entries, err)
	}
}
