package speccontract

import (
	"bytes"
	"errors"
	"os"
	"path/filepath"
	"testing"
)

func TestEmbeddedResourcesMatchCanonical(t *testing.T) {
	files, err := resources("")
	if err != nil {
		t.Fatal(err)
	}
	entries, err := os.ReadDir("../../../spec/1.0.0")
	if err != nil || len(entries) != len(files) {
		t.Fatalf("canonical inventory: %v; embedded=%d canonical=%d", err, len(files), len(entries))
	}
	for name, data := range files {
		want, err := os.ReadFile(filepath.Join("../../../spec/1.0.0", name))
		if err != nil || !bytes.Equal(data, want) {
			t.Fatalf("embedded %s differs: %v", name, err)
		}
	}
	checker, err := NewChecker("")
	if err != nil {
		t.Fatal(err)
	}
	for _, name := range []string{"valid-bundle.json", "valid-future-extension.json"} {
		data, err := os.ReadFile(filepath.Join("../../../spec/fixtures/1.0.0", name))
		if err != nil {
			t.Fatal(err)
		}
		value, err := Decode(data)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := checker.CheckBundle(value.(map[string]any)); err != nil {
			t.Fatalf("embedded checker %s: %v", name, err)
		}
	}
}

func TestOverrideCompatibility(t *testing.T) {
	for _, tc := range []struct{ name, resource, content, code string }{
		{"missing", "event.schema.json", "", "contract_unavailable"},
		{"malformed", "run_context.schema.json", "{", "contract_incompatible"},
		{"changed-schema", "run_context.schema.json", `{}`, "contract_incompatible"},
		{"changed-catalog", "event-types.json", `{}`, "contract_incompatible"},
		{"wrong-version", "capabilities.json", `{"contract_version":"2.0.0"}`, "contract_incompatible"},
		{"same-json", "run_context.schema.json", "whitespace", ""},
	} {
		t.Run(tc.name, func(t *testing.T) {
			root := t.TempDir()
			files, err := resources("")
			if err != nil {
				t.Fatal(err)
			}
			for name, data := range files {
				if name == tc.resource {
					if tc.content == "" {
						continue
					}
					if tc.content == "whitespace" {
						data = append(data, '\n', ' ')
					} else {
						data = []byte(tc.content)
					}
				}
				if err := os.WriteFile(filepath.Join(root, name), data, 0600); err != nil {
					t.Fatal(err)
				}
			}
			_, err = NewChecker(root)
			if tc.code == "" {
				if err != nil {
					t.Fatal(err)
				}
				return
			}
			var ce *ContractError
			if !errors.As(err, &ce) || ce.Code != tc.code {
				t.Fatalf("wanted %s, got %v", tc.code, err)
			}
		})
	}
}
