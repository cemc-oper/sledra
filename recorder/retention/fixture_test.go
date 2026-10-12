package retention

import (
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"
)

func segment(t *testing.T) (string, string, *Manager) {
	t.Helper()
	root := t.TempDir()
	if err := os.Chmod(root, 0700); err != nil {
		t.Fatal(err)
	}
	if err := InitFixture(root); err != nil {
		t.Fatal(err)
	}
	m, err := OpenFixture(root, Policy{SourceRetention: 48 * time.Hour, EvidenceRetention: 240 * time.Hour, MaxBacklogAge: 24 * time.Hour})
	if err != nil {
		t.Fatal(err)
	}
	source, session, uuid := "source-a", "session-a", "123e4567-e89b-42d3-a456-426614174000"
	dir := filepath.Join(root, "spool", digest(source), digest("run"), digest("task"), digest("attempt"))
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, digest(session)+"-"+uuid+".sealed.jsonl")
	b := []byte(`{"record_kind":"segment_header","protocol_version":"1.0.0","source_id":"source-a","segment_uuid":"123e4567-e89b-42d3-a456-426614174000","writer_session":"session-a"}` + "\n" + `{"event_id":"event-a"}` + "\n")
	if err := os.WriteFile(path, b, 0600); err != nil {
		t.Fatal(err)
	}
	old := time.Now().Add(-96 * time.Hour)
	if err := os.Chtimes(path, old, old); err != nil {
		t.Fatal(err)
	}
	return root, path, m
}

func TestFixtureConfirmationCleanupAndRecovery(t *testing.T) {
	root, path, m := segment(t)
	if err := m.Cleanup(path); err == nil || err.Error() != "ack_missing" {
		t.Fatalf("missing ack: %v", err)
	}
	r, err := m.Acknowledge(path)
	if err != nil {
		t.Fatal(err)
	}
	if r.Kind != "fixture_ack" || r.CommittedOffset != r.Size || r.RawLocation == "" {
		t.Fatal(r)
	}
	if _, err := m.Acknowledge(path); err != nil {
		t.Fatalf("repeated ack: %v", err)
	}
	if err := m.Cleanup(path); err == nil || err.Error() != "retention_active" {
		t.Fatalf("early cleanup: %v", err)
	}
	m.now = func() time.Time { return r.AcknowledgedAt.Add(72 * time.Hour) }
	if err := m.Cleanup(path); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(path); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("sealed source still present")
	}
	b, err := os.ReadFile(r.RawLocation)
	if err != nil {
		t.Fatal(err)
	}
	h := sha256.Sum256(b)
	if hex.EncodeToString(h[:]) != r.SHA256 {
		t.Fatal("archive cannot reconstruct source")
	}
	base := filepath.Join(root, "cleanup", digest(r.SourceID), r.SegmentUUID)
	for _, suffix := range []string{".intent.json", ".complete.json"} {
		if _, err := os.Stat(base + suffix); err != nil {
			t.Fatal(err)
		}
	}
}

func TestMismatchMissingArchiveAndExpiryBlockCleanup(t *testing.T) {
	for _, tc := range []string{"digest", "ack", "ack_mismatch", "archive", "expired"} {
		t.Run(tc, func(t *testing.T) {
			root, path, m := segment(t)
			r, err := m.Acknowledge(path)
			if err != nil {
				t.Fatal(err)
			}
			m.now = func() time.Time { return r.AcknowledgedAt.Add(72 * time.Hour) }
			switch tc {
			case "digest":
				if err := os.WriteFile(path, []byte(strings.Repeat("x", int(r.Size))), 0600); err != nil {
					t.Fatal(err)
				}
			case "ack":
				if err := os.Remove(m.ackPath(r)); err != nil {
					t.Fatal(err)
				}
			case "ack_mismatch":
				if err := os.WriteFile(m.ackPath(r), []byte(`{"kind":"fixture_ack","sha256":"wrong"}`), 0600); err != nil {
					t.Fatal(err)
				}
			case "archive":
				if err := os.WriteFile(r.RawLocation, []byte("corrupt"), 0600); err != nil {
					t.Fatal(err)
				}
			case "expired":
				m.now = func() time.Time { return r.AcknowledgedAt.Add(241 * time.Hour) }
			}
			if err := m.Cleanup(path); err == nil {
				t.Fatalf("%s permitted cleanup", tc)
			}
			if _, err := os.Stat(path); err != nil {
				t.Fatal("source lost", err)
			}
			_ = root
		})
	}
}

func TestFixtureIsolationAndUnsafeSegment(t *testing.T) {
	root, path, m := segment(t)
	if _, err := OpenFixture(t.TempDir(), m.policy); err == nil {
		t.Fatal("unmarked root accepted")
	}
	if err := InitFixture(root); err == nil {
		t.Fatal("nonempty root marked again")
	}
	open := strings.TrimSuffix(path, ".sealed.jsonl") + ".open.jsonl"
	if err := os.Link(path, open); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Acknowledge(path); err == nil {
		t.Fatal("dual open/sealed accepted")
	}
	if err := os.Remove(open); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Acknowledge(path); err != nil {
		t.Fatal(err)
	}
}

func TestArchivePublicationFailurePreservesSource(t *testing.T) {
	root, path, m := segment(t)
	if err := os.WriteFile(filepath.Join(root, "fixture-raw"), []byte("not a directory"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := m.Acknowledge(path); err == nil {
		t.Fatal("archive failure accepted")
	}
	if _, err := os.Stat(path); err != nil {
		t.Fatal("source lost", err)
	}
}

func TestConcurrentRepeatedAcknowledgement(t *testing.T) {
	_, path, m := segment(t)
	const n = 8
	var group sync.WaitGroup
	results := make(chan error, n)
	for i := 0; i < n; i++ {
		group.Add(1)
		go func() { defer group.Done(); _, err := m.Acknowledge(path); results <- err }()
	}
	group.Wait()
	close(results)
	for err := range results {
		if err != nil {
			t.Fatal(err)
		}
	}
}
