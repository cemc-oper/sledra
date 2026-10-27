package execution

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
)

func config() RootConfig {
	return RootConfig{FormatVersion, LayoutVersion, Namespace{"mcv", "synthetic"}, "+08:00"}
}

func testRoot(t *testing.T) (*Root, string) {
	t.Helper()
	path := filepath.Join(t.TempDir(), "records")
	if err := InitRoot(path, config()); err != nil {
		t.Fatal(err)
	}
	r, err := OpenRoot(path)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = r.Close() })
	return r, path
}

func TestRootConfigurationIsImmutableAndIndependent(t *testing.T) {
	r, path := testRoot(t)
	before, _ := os.ReadFile(filepath.Join(path, "sledra.json"))
	if r.Config() != config() || InitRoot(path, config()) != nil {
		t.Fatal("idempotent init failed")
	}
	changed := config()
	changed.Namespace.Deployment = "other"
	wantCode(t, InitRoot(path, changed), "root_incompatible")
	after, _ := os.ReadFile(filepath.Join(path, "sledra.json"))
	if !bytes.Equal(before, after) {
		t.Fatal("root settings overwritten")
	}
	entries, _ := os.ReadDir(path)
	if len(entries) != 1 || entries[0].Name() != "sledra.json" {
		t.Fatal("unexpected root metadata")
	}
	for _, name := range []string{"registry", "documents", "contexts", "spool"} {
		t.Run(name, func(t *testing.T) {
			root := t.TempDir()
			if err := os.Mkdir(filepath.Join(root, name), 0o700); err != nil {
				t.Fatal(err)
			}
			wantCode(t, InitRoot(root, config()), "root_incompatible")
			if _, err := os.Stat(filepath.Join(root, "sledra.json")); !os.IsNotExist(err) {
				t.Fatal("legacy root adopted")
			}
		})
	}
}

func TestRootRejectsMissingMixedMalformedAndSpecialFiles(t *testing.T) {
	_, err := OpenRoot(t.TempDir())
	wantCode(t, err, "root_unavailable")
	wantCode(t, InitRoot("relative", config()), "root_invalid")
	for _, raw := range []string{`{}`, `{"format_version":"1.0.0"}`, string(fixture(t, "root.json")) + "garbage"} {
		root := t.TempDir()
		if err := os.WriteFile(filepath.Join(root, "sledra.json"), []byte(raw), 0o600); err != nil {
			t.Fatal(err)
		}
		_, err := OpenRoot(root)
		wantCode(t, err, "root_incompatible")
	}
	r, path := testRoot(t)
	_ = r.Close()
	if err := os.Mkdir(filepath.Join(path, "spool"), 0o700); err != nil {
		t.Fatal(err)
	}
	_, err = OpenRoot(path)
	wantCode(t, err, "root_incompatible")
	root := t.TempDir()
	if err := os.WriteFile(filepath.Join(root, "user-data"), []byte("preserve"), 0o600); err != nil {
		t.Fatal(err)
	}
	wantCode(t, InitRoot(root, config()), "root_incompatible")
	_, target := testRoot(t)
	link := filepath.Join(t.TempDir(), "linked-root")
	if err := os.Symlink(target, link); err != nil {
		t.Fatal(err)
	}
	_, err = OpenRoot(link)
	wantCode(t, err, "root_unavailable")
	wantCode(t, InitRoot(filepath.Join(link, "must-not-create"), config()), "root_unavailable")
	if _, err := os.Stat(filepath.Join(target, "must-not-create")); !os.IsNotExist(err) {
		t.Fatal("initialization followed a symlink ancestor")
	}
	root = t.TempDir()
	if err := os.Symlink(filepath.Join(target, "sledra.json"), filepath.Join(root, "sledra.json")); err != nil {
		t.Fatal(err)
	}
	_, err = OpenRoot(root)
	wantCode(t, err, "root_unavailable")
}

func TestSharedLayoutAndTimezoneVectors(t *testing.T) {
	var vector struct {
		Root           RootConfig
		ExecutionID    string `json:"execution_id"`
		EntryTime      string `json:"entry_time"`
		LaterEventTime string `json:"later_event_time"`
		Path           string
	}
	if err := json.Unmarshal(fixture(t, "layout.json"), &vector); err != nil {
		t.Fatal(err)
	}
	path, err := ExecutionPath(vector.ExecutionID, vector.EntryTime, vector.Root)
	if err != nil || path != vector.Path {
		t.Fatalf("%s: %v", path, err)
	}
	other, _ := ExecutionPath(vector.ExecutionID, vector.LaterEventTime, vector.Root)
	if path == other {
		t.Fatal("test must cross partition date")
	}
	for _, zone := range []string{"+14:01", "+15:00", "-00:00", "Asia/Shanghai", "+08:60"} {
		cfg := config()
		cfg.PartitionTimezone = zone
		_, err := ExecutionPath(vector.ExecutionID, vector.EntryTime, cfg)
		wantCode(t, err, "invalid_timezone")
	}
	_, err = ExecutionPath("../bad", vector.EntryTime, config())
	wantCode(t, err, "schema_invalid")
}
