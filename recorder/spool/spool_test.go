package spool

import (
	"bytes"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"github.com/cemc-oper/sledra/recorder/event"
	"github.com/cemc-oper/sledra/recorder/retention"
)

func fixture(t *testing.T, source, session string) []byte {
	t.Helper()
	b, err := os.ReadFile("../../spec/fixtures/1.0.0/valid-future-extension.json")
	if err != nil {
		t.Fatal(err)
	}
	var bundle struct {
		Events []map[string]any `json:"events"`
	}
	if err := json.Unmarshal(b, &bundle); err != nil {
		t.Fatal(err)
	}
	e := bundle.Events[len(bundle.Events)-1]
	e["source"].(map[string]any)["source_id"] = source
	e["source"].(map[string]any)["session_id"] = session
	e["event_id"], err = event.NewID()
	if err != nil {
		t.Fatal(err)
	}
	b, err = json.Marshal(e)
	if err != nil {
		t.Fatal(err)
	}
	return b
}
func open(t *testing.T, root, source, session string, limits Limits) *Writer {
	t.Helper()
	w, err := Open(root, "future_role", source, "run_eeff24fb97bc287ebc5cddea33eafa8c8e958847ba82e653b501c17fd2662959", "", "", session, limits)
	if err != nil {
		t.Fatal(err)
	}
	return w
}
func segments(t *testing.T, root string) map[string][]byte {
	t.Helper()
	found := map[string][]byte{}
	err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if strings.HasSuffix(path, ".jsonl") {
			b, e := os.ReadFile(path)
			if e != nil {
				return e
			}
			found[path] = b
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return found
}

func TestSessionReuseRotationAndBounds(t *testing.T) {
	root := t.TempDir()
	input := fixture(t, "node-a", "session-a")
	l := Limits{SegmentBytes: event.MaxEventBytes + 512, SessionBytes: 1 << 20, Segments: 2, SegmentAge: time.Hour}
	w := open(t, root, "node-a", "session-a", l)
	for i := 0; i < 3; i++ {
		if err := w.Append(input); err != nil {
			t.Fatal(err)
		}
	}
	if w.Stats().Written != 3 {
		t.Fatal(w.Stats())
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	w = open(t, root, "node-a", "session-a", l)
	if err := w.Append(input); err != nil {
		t.Fatal(err)
	}
	if err := w.Seal(); err != nil {
		t.Fatal(err)
	}
	if err := w.Append(input); err != nil {
		t.Fatal(err)
	}
	if err := w.Seal(); err != nil {
		t.Fatal(err)
	}
	if code := Code(w.Append(input)); code != "spool_full" {
		t.Fatal(code)
	}
	_ = w.Close()
	files := segments(t, root)
	if len(files) != 2 {
		t.Fatalf("segments=%d", len(files))
	}
	for path, b := range files {
		if !strings.HasSuffix(path, ".sealed.jsonl") || b[len(b)-1] != '\n' || bytes.Count(b, []byte("\n")) < 2 {
			t.Fatalf("invalid segment %s", path)
		}
		for _, line := range bytes.Split(bytes.TrimSuffix(b, []byte("\n")), []byte("\n")) {
			if !json.Valid(line) {
				t.Fatalf("interleaved JSON in %s", path)
			}
		}
	}
}

func TestGlobalCapacityStopsNewFilesAndBytes(t *testing.T) {
	root := t.TempDir()
	limits := Limits{MaxSpoolBytes: 1 << 20, MaxSpoolFiles: 3}
	w := open(t, root, "node-a", "session-a", limits)
	if err := w.Append(fixture(t, "node-a", "session-a")); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(root, "future_role", "node-b", "run_eeff24fb97bc287ebc5cddea33eafa8c8e958847ba82e653b501c17fd2662959", "", "", "session-b", limits); Code(err) != "spool_full" {
		t.Fatalf("file cap: %v", err)
	}
	if len(segments(t, root)) != 1 {
		t.Fatal("second source created segment")
	}
	root = t.TempDir()
	limits = Limits{MaxSpoolBytes: event.MaxEventBytes + 1024, MaxSpoolFiles: 100}
	w = open(t, root, "node-a", "session-a", limits)
	input := fixture(t, "node-a", "session-a")
	var stopped bool
	for i := 0; i < 500; i++ {
		if err := w.Append(input); err != nil {
			if Code(err) != "spool_full" {
				t.Fatal(err)
			}
			stopped = true
			break
		}
	}
	if !stopped {
		t.Fatal("byte cap did not stop appends")
	}
	before := segments(t, root)
	if Code(w.Append(input)) != "spool_full" {
		t.Fatal("append resumed above cap")
	}
	after := segments(t, root)
	if len(before) != len(after) {
		t.Fatal("capacity failure created file")
	}
	for path, b := range before {
		if !bytes.Equal(b, after[path]) {
			t.Fatal("capacity failure changed segment")
		}
	}
	_ = w.Close()
}

func TestConcurrentSourcesAndLockConflict(t *testing.T) {
	root := t.TempDir()
	const n = 12
	var group sync.WaitGroup
	errs := make(chan error, n)
	for i := 0; i < n; i++ {
		group.Add(1)
		go func(i int) {
			defer group.Done()
			source := "node-" + string(rune('a'+i))
			w, err := Open(root, "future_role", source, "run_eeff24fb97bc287ebc5cddea33eafa8c8e958847ba82e653b501c17fd2662959", "", "", "session-a", Limits{})
			if err == nil {
				err = w.Append(fixture(t, source, "session-a"))
				if e := w.Close(); err == nil {
					err = e
				}
			}
			errs <- err
		}(i)
	}
	group.Wait()
	close(errs)
	for err := range errs {
		if err != nil {
			t.Fatal(err)
		}
	}
	if got := len(segments(t, root)); got != n {
		t.Fatalf("segments=%d", got)
	}
	var dirs, locks, events int
	if err := filepath.WalkDir(root, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			dirs++
		} else if strings.HasSuffix(path, ".lock") {
			locks++
		} else if strings.HasSuffix(path, ".jsonl") {
			b, e := os.ReadFile(path)
			if e != nil {
				return e
			}
			events += bytes.Count(b, []byte("\n")) - 1
		}
		return nil
	}); err != nil {
		t.Fatal(err)
	}
	if dirs != 2+4*n || locks != n || events != n {
		t.Fatalf("dirs=%d locks=%d events=%d", dirs, locks, events)
	}
	w := open(t, root, "node-a", "session-a", Limits{})
	_, err := Open(root, "future_role", "node-a", "run_eeff24fb97bc287ebc5cddea33eafa8c8e958847ba82e653b501c17fd2662959", "", "", "session-a", Limits{})
	if Code(err) != "writer_busy" {
		t.Fatalf("lock conflict: %v", err)
	}
	_ = w.Close()
}

func TestPartialTailAndModes(t *testing.T) {
	root := t.TempDir()
	input := fixture(t, "node-a", "session-a")
	w := open(t, root, "node-a", "session-a", Limits{})
	w.write = func(f *os.File, b []byte) (int, error) { return f.Write(b[:len(b)/2]) }
	if Code(w.Append(input)) != "spool_unavailable" {
		t.Fatal("partial append not reported")
	}
	if Code(w.Append(input)) != "spool_unavailable" {
		t.Fatal("poisoned writer reused")
	}
	_ = w.Close()
	w = open(t, root, "node-a", "session-a", Limits{})
	if err := w.Record(input, false); err != nil {
		t.Fatal(err)
	}
	if err := w.Record([]byte("invalid"), false); err != nil {
		t.Fatal(err)
	}
	if w.Stats().Dropped != 1 || w.Stats().LastError == "" {
		t.Fatal(w.Stats())
	}
	if w.Record([]byte("invalid"), true) == nil {
		t.Fatal("strict error suppressed")
	}
	_ = w.Close()
	var partial, complete int
	for _, b := range segments(t, root) {
		if b[len(b)-1] == '\n' {
			complete++
		} else {
			partial++
			lastLF := bytes.LastIndexByte(b, '\n')
			if lastLF < 0 || lastLF == len(b)-1 || !json.Valid(b[:lastLF]) {
				t.Fatal("half-line offset not locatable")
			}
		}
	}
	if partial != 1 || complete != 1 {
		t.Fatalf("partial=%d complete=%d", partial, complete)
	}
}

func TestCapacityOversizeAndUnusableRoot(t *testing.T) {
	root := t.TempDir()
	input := fixture(t, "node-a", "session-a")
	w := open(t, root, "node-a", "session-a", Limits{SegmentBytes: event.MaxEventBytes + 512, SessionBytes: event.MaxEventBytes + 512, Segments: 1})
	if Code(w.Append(bytes.Repeat([]byte("x"), event.MaxEventBytes+1))) != "limit_exceeded" {
		t.Fatal("oversize")
	}
	if len(segments(t, root)) != 0 {
		t.Fatal("invalid event created segment")
	}
	for i := 0; i < 200; i++ {
		if err := w.Append(input); err != nil {
			if Code(err) != "spool_full" {
				t.Fatal(err)
			}
			break
		}
		if i == 199 {
			t.Fatal("quota not applied")
		}
	}
	_ = w.Close()
	link := filepath.Join(t.TempDir(), "root")
	if err := os.Symlink(root, link); err != nil {
		t.Fatal(err)
	}
	if _, err := Open(link, "future_role", "node", "run", "", "", "session", Limits{}); Code(err) != "spool_unavailable" {
		t.Fatalf("symlink: %v", err)
	}
	if _, err := Open(filepath.Join(root, "missing"), "future_role", "node", "run", "", "", "session", Limits{}); Code(err) != "spool_unavailable" {
		t.Fatalf("missing root: %v", err)
	}
	denied := t.TempDir()
	if err := os.Chmod(denied, 0500); err != nil {
		t.Fatal(err)
	}
	defer os.Chmod(denied, 0700)
	if _, err := Open(denied, "future_role", "node", "run", "", "", "session", Limits{}); Code(err) != "spool_unavailable" {
		t.Fatalf("permission: %v", err)
	}
	// Inject a write failure independently of directory permissions.
	w = open(t, root, "node-b", "session-b", Limits{})
	w.write = func(_ *os.File, _ []byte) (int, error) { return 0, io.ErrClosedPipe }
	if Code(w.Append(fixture(t, "node-b", "session-b"))) != "spool_unavailable" {
		t.Fatal("write error")
	}
	_ = w.Close()
}

func TestWrongAssociationAndSeparateSession(t *testing.T) {
	root := t.TempDir()
	w := open(t, root, "node-a", "session-a", Limits{})
	if Code(w.Append(fixture(t, "node-b", "session-a"))) != "parent_mismatch" {
		t.Fatal("source mismatch")
	}
	if Code(w.Append(fixture(t, "node-a", "session-b"))) != "parent_mismatch" {
		t.Fatal("session mismatch")
	}
	if err := w.Close(); err != nil && !errors.Is(err, os.ErrClosed) {
		t.Fatal(err)
	}
	if len(segments(t, root)) != 0 {
		t.Fatal("mismatched event persisted")
	}
}

func TestResumePublicationAndAge(t *testing.T) {
	for _, mode := range []string{"dual", "conflict", "aged", "partial"} {
		t.Run(mode, func(t *testing.T) {
			root := t.TempDir()
			input := fixture(t, "node-a", "session-a")
			limits := Limits{SegmentAge: time.Hour}
			w := open(t, root, "node-a", "session-a", limits)
			if err := w.Append(input); err != nil {
				t.Fatal(err)
			}
			old := filepath.Join(w.dir, w.fileName)
			if err := w.Close(); err != nil {
				t.Fatal(err)
			}
			sealed := strings.TrimSuffix(old, ".open.jsonl") + ".sealed.jsonl"
			switch mode {
			case "dual":
				if err := os.Link(old, sealed); err != nil {
					t.Fatal(err)
				}
			case "conflict":
				if err := os.WriteFile(sealed, []byte("conflicting sealed bytes\n"), 0600); err != nil {
					t.Fatal(err)
				}
			case "aged":
				limits.SegmentAge = time.Nanosecond
			case "partial":
				f, err := os.OpenFile(old, os.O_APPEND|os.O_WRONLY, 0)
				if err != nil {
					t.Fatal(err)
				}
				_, err = f.WriteString("half line")
				f.Close()
				if err != nil {
					t.Fatal(err)
				}
				limits.SegmentAge = time.Nanosecond
			}
			before := segments(t, root)
			oldHash := sha256.Sum256(before[old])
			if mode == "conflict" {
				if _, err := Open(root, "future_role", "node-a", "run_eeff24fb97bc287ebc5cddea33eafa8c8e958847ba82e653b501c17fd2662959", "", "", "session-a", limits); Code(err) != "spool_unavailable" {
					t.Fatalf("conflict accepted: %v", err)
				}
				if got := segments(t, root); !bytes.Equal(got[old], before[old]) || !bytes.Equal(got[sealed], before[sealed]) {
					t.Fatal("conflict evidence changed")
				}
				return
			}
			for i := 0; i < 2; i++ {
				w = open(t, root, "node-a", "session-a", limits)
				if i == 1 {
					if err := w.Append(input); err != nil {
						t.Fatal(err)
					}
				}
				if err := w.Close(); err != nil {
					t.Fatal(err)
				}
			}
			got := segments(t, root)
			if mode == "partial" {
				if sha256.Sum256(got[old]) != oldHash || len(got) != 2 {
					t.Fatal("partial tail changed or new segment missing")
				}
			} else if _, exists := got[old]; exists || sha256.Sum256(got[sealed]) != oldHash || len(got) != 2 {
				t.Fatal("old complete segment not sealed immutably")
			}
		})
	}
}

func TestKillBetweenSealLinkAndUnlink(t *testing.T) {
	if os.Getenv("SLEDRA_SEAL_CHILD") == "1" {
		root := os.Getenv("SLEDRA_SEAL_ROOT")
		w := open(t, root, "node-a", "session-a", Limits{})
		if err := w.Append(fixture(t, "node-a", "session-a")); err != nil {
			t.Fatal(err)
		}
		w.afterSealLink = func() { _ = syscall.Kill(os.Getpid(), syscall.SIGKILL) }
		_ = w.Seal()
		t.Fatal("child survived seal cut")
	}
	root := t.TempDir()
	if err := os.Chmod(root, 0700); err != nil {
		t.Fatal(err)
	}
	if err := retention.InitFixture(root); err != nil {
		t.Fatal(err)
	}
	cmd := exec.Command(os.Args[0], "-test.run=^TestKillBetweenSealLinkAndUnlink$")
	cmd.Env = append(os.Environ(), "SLEDRA_SEAL_CHILD=1", "SLEDRA_SEAL_ROOT="+root)
	if output, err := cmd.CombinedOutput(); err == nil {
		t.Fatalf("child survived: %s", output)
	} else if exit, ok := err.(*exec.ExitError); !ok || exit.ProcessState.Sys().(syscall.WaitStatus).Signal() != syscall.SIGKILL {
		t.Fatalf("child failed before cut: %v %s", err, output)
	}
	var old, sealed string
	for path := range segments(t, root) {
		if strings.HasSuffix(path, ".open.jsonl") {
			old = path
			sealed = strings.TrimSuffix(path, ".open.jsonl") + ".sealed.jsonl"
		}
	}
	if old == "" {
		t.Fatal("missing open at cut")
	}
	before, err := os.ReadFile(sealed)
	if err != nil {
		t.Fatal(err)
	}
	w := open(t, root, "node-a", "session-a", Limits{})
	if err := w.Append(fixture(t, "node-a", "session-a")); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(old); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("open alias survived recovery")
	}
	after, err := os.ReadFile(sealed)
	if err != nil || sha256.Sum256(after) != sha256.Sum256(before) {
		t.Fatal("sealed bytes changed")
	}
	m, err := retention.OpenFixture(root, retention.Policy{SourceRetention: 48 * time.Hour, EvidenceRetention: 240 * time.Hour, MaxBacklogAge: 24 * time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	receipt, err := m.Acknowledge(sealed)
	if err != nil || receipt.SHA256 != fmt.Sprintf("%x", sha256.Sum256(before)) {
		t.Fatalf("archive confirmation: %v %+v", err, receipt)
	}
}

func TestNewerPartialBlocksOlderResume(t *testing.T) {
	root := t.TempDir()
	input := fixture(t, "node-a", "session-a")
	w := open(t, root, "node-a", "session-a", Limits{})
	if err := w.Append(input); err != nil {
		t.Fatal(err)
	}
	older := filepath.Join(w.dir, w.fileName)
	if err := w.Seal(); err != nil {
		t.Fatal(err)
	}
	if err := w.Append(input); err != nil {
		t.Fatal(err)
	}
	newer := filepath.Join(w.dir, w.fileName)
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	// Restore the older sealed name as an open segment, then corrupt the
	// newer open tail. Recovery must create a third segment.
	olderOpen := strings.TrimSuffix(older, ".open.jsonl") + ".sealed.jsonl"
	if err := os.Rename(olderOpen, older); err != nil {
		t.Fatal(err)
	}
	oldTime := time.Now().Add(-time.Minute)
	if err := os.Chtimes(older, oldTime, oldTime); err != nil {
		t.Fatal(err)
	}
	f, err := os.OpenFile(newer, os.O_APPEND|os.O_WRONLY, 0)
	if err != nil {
		t.Fatal(err)
	}
	_, err = f.WriteString("half line")
	f.Close()
	if err != nil {
		t.Fatal(err)
	}
	before, err := os.ReadFile(older)
	if err != nil {
		t.Fatal(err)
	}
	w = open(t, root, "node-a", "session-a", Limits{})
	if err := w.Append(input); err != nil {
		t.Fatal(err)
	}
	if err := w.Close(); err != nil {
		t.Fatal(err)
	}
	after, err := os.ReadFile(older)
	if err != nil || !bytes.Equal(before, after) || len(segments(t, root)) != 3 {
		t.Fatal("resumed older segment past partial tail")
	}
}

func TestSealConflictPreservesBothNames(t *testing.T) {
	root := t.TempDir()
	w := open(t, root, "node-a", "session-a", Limits{})
	if err := w.Append(fixture(t, "node-a", "session-a")); err != nil {
		t.Fatal(err)
	}
	openPath := filepath.Join(w.dir, w.fileName)
	sealed := strings.TrimSuffix(openPath, ".open.jsonl") + ".sealed.jsonl"
	if err := os.WriteFile(sealed, []byte("conflict\n"), 0600); err != nil {
		t.Fatal(err)
	}
	before := segments(t, root)
	if Code(w.Seal()) != "spool_unavailable" || Code(w.Append(fixture(t, "node-a", "session-a"))) != "spool_unavailable" {
		t.Fatal("seal conflict did not poison writer")
	}
	_ = w.Close()
	after := segments(t, root)
	if !bytes.Equal(before[openPath], after[openPath]) || !bytes.Equal(before[sealed], after[sealed]) {
		t.Fatal("seal conflict changed evidence")
	}
}

func TestShortLivedWritersSealAgedOpen(t *testing.T) {
	root := t.TempDir()
	input := fixture(t, "node-a", "session-a")
	limits := Limits{SegmentAge: time.Nanosecond}
	for i := 0; i < 4; i++ {
		w := open(t, root, "node-a", "session-a", limits)
		if err := w.Append(input); err != nil {
			t.Fatal(err)
		}
		if err := w.Close(); err != nil {
			t.Fatal(err)
		}
		var openCount, sealedCount int
		for path := range segments(t, root) {
			if strings.HasSuffix(path, ".open.jsonl") {
				openCount++
			} else {
				sealedCount++
			}
		}
		if openCount != 1 || sealedCount != i {
			t.Fatalf("iteration %d: open=%d sealed=%d", i, openCount, sealedCount)
		}
	}
}
