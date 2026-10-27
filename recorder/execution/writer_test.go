package execution

import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
)

func start(t *testing.T, r *Root) ([]byte, map[string]any) {
	t.Helper()
	raw, err := r.codec.NewStart(nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	m, _ := r.codec.Validate("event", raw)
	return raw, m
}

func fact(t *testing.T, r *Root, id string) []byte {
	t.Helper()
	raw, err := r.codec.NewEvent(id, "test.progress", map[string]any{"step": json.Number("9007199254740993"), "fraction": json.Number("0.125")})
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func TestWriterFrozenDateAndNoOverwrite(t *testing.T) {
	r, root := testRoot(t)
	raw := bytes.Split(fixture(t, "unresolved.jsonl"), []byte{'\n'})[0]
	w, err := r.Create(raw)
	if err != nil {
		t.Fatal(err)
	}
	path := w.Path()
	id := w.id
	context, _ := r.codec.Validate("event", fixture(t, "context-none.json"))
	context["occurred_at"] = "2026-10-28T09:00:00+08:00"
	encoded, _ := r.codec.Encode("event", context)
	if err := w.Append(encoded); err != nil {
		t.Fatal(err)
	}
	if w.Path() != path || !strings.Contains(path, "/20261027/") {
		t.Fatal("file moved at midnight/context")
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	before, _ := os.ReadFile(filepath.Join(root, path))
	if _, err := r.Create(raw); err == nil {
		t.Fatal("execution file overwritten")
	}
	after, _ := os.ReadFile(filepath.Join(root, path))
	if !bytes.Equal(before, after) {
		t.Fatal("exclusive create changed bytes")
	}
	opened, err := r.OpenWriter(id, "2026-10-27T15:59:59.123456789Z")
	if err != nil {
		t.Fatal(err)
	}
	defer opened.Close()
	if err := opened.Append(fact(t, r, id)); err != nil {
		t.Fatal(err)
	}
}

func TestManyExecutionsHaveSeparateFiles(t *testing.T) {
	r, root := testRoot(t)
	var wg sync.WaitGroup
	errors := make(chan error, 32)
	paths := make(chan string, 32)
	for range 32 {
		wg.Add(1)
		go func() {
			defer wg.Done()
			raw, err := r.codec.NewStart(nil, nil)
			if err != nil {
				errors <- err
				return
			}
			w, err := r.Create(raw)
			if err != nil {
				errors <- err
				return
			}
			paths <- w.Path()
			errors <- w.Close()
		}()
	}
	wg.Wait()
	close(errors)
	close(paths)
	for err := range errors {
		if err != nil {
			t.Fatal(err)
		}
	}
	seen := map[string]bool{}
	for path := range paths {
		if seen[path] {
			t.Fatal("shared file")
		}
		seen[path] = true
	}
	files := 0
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if !d.IsDir() {
			files++
			if d.Name() != "sledra.json" {
				if !strings.HasSuffix(path, ".jsonl") || !seen[strings.TrimPrefix(path, root+"/")] {
					return fmt.Errorf("unexpected file %s", path)
				}
				data, _ := os.ReadFile(path)
				if bytes.Count(data, []byte{'\n'}) != 1 {
					return fmt.Errorf("mixed starts %s", path)
				}
			}
		}
		return nil
	})
	if err != nil || files != 33 || len(seen) != 32 {
		t.Fatalf("files=%d executions=%d: %v", files, len(seen), err)
	}
}

func TestShortWriteAndUncertainResultDisableWriter(t *testing.T) {
	for _, mode := range []string{"short", "complete-with-error", "zero"} {
		t.Run(mode, func(t *testing.T) {
			r, root := testRoot(t)
			raw, _ := start(t, r)
			w, err := r.Create(raw)
			if err != nil {
				t.Fatal(err)
			}
			defer w.Close()
			calls := 0
			w.write = func(line []byte) (int, error) {
				calls++
				switch mode {
				case "short":
					return w.file.Write(line[:7])
				case "complete-with-error":
					n, _ := w.file.Write(line)
					return n, io.ErrUnexpectedEOF
				default:
					return 0, syscall.ENOSPC
				}
			}
			wantCode(t, w.Append(fact(t, r, w.id)), "indeterminate")
			before, _ := os.ReadFile(filepath.Join(root, w.Path()))
			wantCode(t, w.Append(fact(t, r, w.id)), "writer_disabled")
			after, _ := os.ReadFile(filepath.Join(root, w.Path()))
			if calls != 1 || !bytes.Equal(before, after) {
				t.Fatal("continued after failed write")
			}
			if mode == "short" {
				_, err := r.OpenWriter(w.id, stringTime(t, r, raw))
				wantCode(t, err, "incomplete_tail")
			}
		})
	}
}

func stringTime(t *testing.T, r *Root, raw []byte) string {
	t.Helper()
	m, err := r.codec.Validate("event", raw)
	if err != nil {
		t.Fatal(err)
	}
	return m["occurred_at"].(string)
}

func TestBadTailCloseAndValidationDoNotContinue(t *testing.T) {
	r, root := testRoot(t)
	raw, _ := start(t, r)
	w, err := r.Create(raw)
	if err != nil {
		t.Fatal(err)
	}
	wantCode(t, w.Append([]byte(`{}`)), "unsupported_version")
	id, _ := NewID()
	wantCode(t, w.Append(fact(t, r, id)), "execution_mismatch")
	if err := w.Append(fact(t, r, w.id)); err != nil {
		t.Fatal("prewrite rejection poisoned writer", err)
	}
	if _, err := w.file.Write([]byte("half")); err != nil {
		t.Fatal(err)
	}
	wantCode(t, w.Append(fact(t, r, w.id)), "incomplete_tail")
	wantCode(t, w.Append(fact(t, r, w.id)), "writer_disabled")
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	other, _ := start(t, r)
	closed, err := r.Create(other)
	if err != nil {
		t.Fatal(err)
	}
	closed.closeFile = func() error { _ = closed.file.Close(); return syscall.EIO }
	wantCode(t, closed.Close(), "indeterminate")
	wantCode(t, closed.Close(), "indeterminate")
	wantCode(t, closed.Append(fact(t, r, closed.id)), "writer_disabled")
	if err := os.Remove(filepath.Join(root, closed.Path())); err != nil {
		t.Fatal(err)
	}
	_, err = r.OpenWriter(closed.id, stringTime(t, r, other))
	wantCode(t, err, "io_unavailable")
}

func TestWriterRejectsLinksAndSpecialFiles(t *testing.T) {
	r, root := testRoot(t)
	raw, m := start(t, r)
	path, _ := ExecutionPath(m["execution_id"].(string), m["occurred_at"].(string), config())
	if err := os.Symlink(t.TempDir(), filepath.Join(root, "executions")); err != nil {
		t.Fatal(err)
	}
	_, err := r.Create(raw)
	wantCode(t, err, "io_unavailable")
	_ = os.Remove(filepath.Join(root, "executions"))
	if err := os.MkdirAll(filepath.Join(root, filepath.Dir(path)), 0o700); err != nil {
		t.Fatal(err)
	}
	if err := syscall.Mkfifo(filepath.Join(root, path), 0o600); err != nil {
		t.Fatal(err)
	}
	_, err = r.OpenWriter(m["execution_id"].(string), m["occurred_at"].(string))
	wantCode(t, err, "io_unavailable")
	_ = os.Remove(filepath.Join(root, path))
	if err := os.WriteFile(filepath.Join(root, path), []byte("fake\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(filepath.Join(root, path), filepath.Join(root, "another-file")); err != nil {
		t.Fatal(err)
	}
	_, err = r.OpenWriter(m["execution_id"].(string), m["occurred_at"].(string))
	wantCode(t, err, "io_unavailable")
}

func TestReadRecordIsBounded(t *testing.T) {
	_, err := ReadRecord(strings.NewReader(strings.Repeat("x", MaxRecordBytes+1)))
	wantCode(t, err, "limit_exceeded")
	if raw, err := ReadRecord(strings.NewReader("{}")); err != nil || string(raw) != "{}" {
		t.Fatal("bounded read", err)
	}
}

func TestLateCycleKindsNeverRelocateFiles(t *testing.T) {
	for _, kind := range []string{"single", "none", "multiple"} {
		t.Run(kind, func(t *testing.T) {
			r, _ := testRoot(t)
			raw, event := start(t, r)
			event["occurred_at"] = "2026-10-27T23:59:59+08:00"
			raw, _ = r.codec.Encode("event", event)
			w, err := r.Create(raw)
			if err != nil {
				t.Fatal(err)
			}
			defer w.Close()
			before := w.Path()
			context, err := r.codec.Validate("event", fixture(t, "context-none.json"))
			if err != nil {
				t.Fatal(err)
			}
			context["execution_id"] = w.id
			context["occurred_at"] = "2026-10-28T09:00:00+08:00"
			attrs := context["attributes"].(map[string]any)
			attrs["cycle_kind"] = kind
			if kind == "single" {
				attrs["cycle"] = "2024-07-01T00:00:00+08:00"
			}
			encoded, err := r.codec.Encode("event", context)
			if err != nil {
				t.Fatal(err)
			}
			if err := w.Append(encoded); err != nil || w.Path() != before {
				t.Fatalf("late context moved file: %v", err)
			}
		})
	}
}

func TestPermissionFailureDoesNotCreateExecution(t *testing.T) {
	if os.Geteuid() == 0 {
		t.Skip("permission bits cannot reject a privileged writer")
	}
	r, root := testRoot(t)
	dir := filepath.Join(root, "executions")
	if err := os.Mkdir(dir, 0o500); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chmod(dir, 0o700) })
	raw, _ := start(t, r)
	_, err := r.Create(raw)
	wantCode(t, err, "io_unavailable")
	entries, _ := os.ReadDir(dir)
	if len(entries) != 0 {
		t.Fatal("failed execution created a file")
	}
}
