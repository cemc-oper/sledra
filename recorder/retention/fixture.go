// Package retention provides an isolated confirmation fixture. Production
// acknowledgements must come from the SLD-13 committed raw/checkpoint transaction.
package retention

import (
	"bufio"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"strings"
	"syscall"
	"time"
)

const marker = ".sledra-retention-fixture"

type Policy struct {
	SourceRetention   time.Duration
	EvidenceRetention time.Duration
	MaxBacklogAge     time.Duration
}

type Receipt struct {
	Kind            string    `json:"kind"`
	SourceID        string    `json:"source_id"`
	SegmentUUID     string    `json:"segment_uuid"`
	SHA256          string    `json:"sha256"`
	Size            int64     `json:"size"`
	RawLocation     string    `json:"raw_location"`
	CommittedOffset int64     `json:"committed_offset"`
	AcknowledgedAt  time.Time `json:"acknowledged_at"`
}

type Manager struct {
	root   string
	policy Policy
	now    func() time.Time
}

// InitFixture only marks an empty, private temporary root; it refuses a root
// with an existing spool or any other contents.
func InitFixture(root string) error {
	root, err := filepath.Abs(root)
	if err != nil {
		return err
	}
	st, err := os.Lstat(root)
	if err != nil || !st.IsDir() || st.Mode().Perm() != 0700 {
		return errors.New("fixture_root_invalid")
	}
	entries, err := os.ReadDir(root)
	if err != nil || len(entries) != 0 {
		return errors.New("fixture_root_not_empty")
	}
	f, err := os.OpenFile(filepath.Join(root, marker), os.O_WRONLY|os.O_CREATE|os.O_EXCL|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return err
	}
	if err = f.Sync(); err != nil {
		f.Close()
		return err
	}
	if err = f.Close(); err != nil {
		return err
	}
	return syncDir(root)
}

func OpenFixture(root string, p Policy) (*Manager, error) {
	root, err := filepath.Abs(root)
	if err != nil {
		return nil, err
	}
	if p.SourceRetention <= 0 || p.EvidenceRetention < p.SourceRetention || p.MaxBacklogAge <= 0 || p.SourceRetention <= p.MaxBacklogAge {
		return nil, errors.New("policy_invalid")
	}
	st, err := os.Lstat(root)
	if err != nil || !st.IsDir() || st.Mode().Perm() != 0700 {
		return nil, errors.New("fixture_root_invalid")
	}
	f, err := os.OpenFile(filepath.Join(root, marker), os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return nil, errors.New("fixture_required")
	}
	st, err = f.Stat()
	f.Close()
	if err != nil || !st.Mode().IsRegular() || st.Mode().Perm() != 0600 {
		return nil, errors.New("fixture_required")
	}
	return &Manager{root: root, policy: p, now: time.Now}, nil
}

func (m *Manager) sealed(path string) (Receipt, os.FileInfo, error) {
	var r Receipt
	abs, err := filepath.Abs(path)
	if err != nil {
		return r, nil, err
	}
	rel, err := filepath.Rel(filepath.Join(m.root, "spool"), abs)
	if err != nil || rel == "." || rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) || !strings.HasSuffix(rel, ".sealed.jsonl") {
		return r, nil, errors.New("path_invalid")
	}
	parts := strings.Split(rel, string(os.PathSeparator))
	if len(parts) != 5 {
		return r, nil, errors.New("path_invalid")
	}
	cur := m.root
	for _, part := range append([]string{"spool"}, parts[:len(parts)-1]...) {
		cur = filepath.Join(cur, part)
		st, e := os.Lstat(cur)
		if e != nil || !st.IsDir() || st.Mode().Perm() != 0700 {
			return r, nil, errors.New("path_invalid")
		}
	}
	f, err := os.OpenFile(abs, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return r, nil, err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil || !st.Mode().IsRegular() || st.Mode().Perm() != 0600 || st.Size() < 2 {
		return r, nil, errors.New("segment_invalid")
	}
	if stat, ok := st.Sys().(*syscall.Stat_t); !ok || stat.Nlink != 1 {
		return r, nil, errors.New("segment_not_exclusive")
	}
	open := strings.TrimSuffix(abs, ".sealed.jsonl") + ".open.jsonl"
	if _, err := os.Lstat(open); err == nil {
		return r, nil, errors.New("segment_open_duplicate")
	} else if !errors.Is(err, os.ErrNotExist) {
		return r, nil, err
	}
	h := sha256.New()
	line, err := bufio.NewReader(io.LimitReader(f, 4096)).ReadBytes('\n')
	if err != nil {
		return r, nil, errors.New("header_invalid")
	}
	var hdr struct {
		RecordKind      string `json:"record_kind"`
		ProtocolVersion string `json:"protocol_version"`
		SourceID        string `json:"source_id"`
		SegmentUUID     string `json:"segment_uuid"`
		WriterSession   string `json:"writer_session"`
	}
	if json.Unmarshal(line, &hdr) != nil || hdr.RecordKind != "segment_header" || hdr.ProtocolVersion != "1.0.0" || hdr.SourceID == "" || hdr.SegmentUUID == "" || parts[0] != digest(hdr.SourceID) || parts[4] != digest(hdr.WriterSession)+"-"+hdr.SegmentUUID+".sealed.jsonl" {
		return r, nil, errors.New("header_invalid")
	}
	if _, err = f.Seek(-1, io.SeekEnd); err != nil {
		return r, nil, err
	}
	var last [1]byte
	if _, err = io.ReadFull(f, last[:]); err != nil || last[0] != '\n' {
		return r, nil, errors.New("segment_incomplete")
	}
	// Rehash including the header after tail verification.
	if _, err = f.Seek(0, io.SeekStart); err != nil {
		return r, nil, err
	}
	h.Reset()
	if _, err = io.Copy(h, f); err != nil {
		return r, nil, err
	}
	r = Receipt{Kind: "fixture_ack", SourceID: hdr.SourceID, SegmentUUID: hdr.SegmentUUID, SHA256: hex.EncodeToString(h.Sum(nil)), Size: st.Size(), CommittedOffset: st.Size()}
	return r, st, nil
}

func digest(s string) string { x := sha256.Sum256([]byte(s)); return hex.EncodeToString(x[:]) }
func syncDir(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	return f.Sync()
}
func ensure(path string) error {
	if err := os.MkdirAll(path, 0700); err != nil {
		return err
	}
	st, err := os.Lstat(path)
	if err != nil || !st.IsDir() || st.Mode().Perm() != 0700 {
		return errors.New("path_invalid")
	}
	return nil
}

func publish(path string, content []byte) error {
	dir := filepath.Dir(path)
	if err := ensure(dir); err != nil {
		return err
	}
	tmp, err := os.CreateTemp(dir, ".receipt-*")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if err = tmp.Chmod(0600); err != nil {
		tmp.Close()
		return err
	}
	if _, err = tmp.Write(content); err == nil {
		err = tmp.Sync()
	}
	if e := tmp.Close(); err == nil {
		err = e
	}
	if err != nil {
		return err
	}
	if err = os.Link(tmp.Name(), path); err != nil {
		if errors.Is(err, os.ErrExist) {
			existing, e := os.ReadFile(path)
			if e == nil && string(existing) == string(content) {
				return nil
			}
			return errors.New("receipt_conflict")
		}
		return err
	}
	return syncDir(dir)
}

// Acknowledge copies a sealed segment into a fixture raw archive before
// publishing an immutable synthetic receipt. It is not a real ingestion ack.
func (m *Manager) Acknowledge(path string) (Receipt, error) {
	r, _, err := m.sealed(path)
	if err != nil {
		return r, err
	}
	archive := filepath.Join(m.root, "fixture-raw", digest(r.SourceID), r.SegmentUUID+".jsonl")
	if err = ensure(filepath.Dir(archive)); err != nil {
		return r, err
	}
	if _, err = os.Lstat(archive); errors.Is(err, os.ErrNotExist) {
		src, e := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
		if e != nil {
			return r, e
		}
		tmp, e := os.CreateTemp(filepath.Dir(archive), ".archive-*")
		if e != nil {
			src.Close()
			return r, e
		}
		defer os.Remove(tmp.Name())
		_, e = io.Copy(tmp, src)
		src.Close()
		if e == nil {
			e = tmp.Sync()
		}
		if x := tmp.Close(); e == nil {
			e = x
		}
		if e != nil {
			return r, e
		}
		if e = os.Link(tmp.Name(), archive); e != nil && !errors.Is(e, os.ErrExist) {
			return r, e
		}
		if e = syncDir(filepath.Dir(archive)); e != nil {
			return r, e
		}
	} else if err != nil {
		return r, err
	}
	if err = verifyArchive(archive, r); err != nil {
		return r, err
	}
	r.RawLocation = archive
	r.AcknowledgedAt = m.now().UTC()
	ack := m.ackPath(r)
	if old, e := os.ReadFile(ack); e == nil {
		var existing Receipt
		if json.Unmarshal(old, &existing) == nil && same(existing, r) && existing.RawLocation == archive {
			return existing, nil
		}
		return r, errors.New("receipt_conflict")
	} else if !errors.Is(e, os.ErrNotExist) {
		return r, e
	}
	b, _ := json.Marshal(r)
	b = append(b, '\n')
	if err = publish(ack, b); err != nil {
		// A concurrent identical acknowledgement can win publication with a
		// different wall-clock timestamp; its immutable receipt is canonical.
		if old, readErr := os.ReadFile(ack); readErr == nil {
			var existing Receipt
			if json.Unmarshal(old, &existing) == nil && same(existing, r) && existing.RawLocation == archive {
				return existing, nil
			}
		}
		return r, err
	}
	return r, nil
}

func same(a, b Receipt) bool {
	return a.Kind == b.Kind && a.SourceID == b.SourceID && a.SegmentUUID == b.SegmentUUID && a.SHA256 == b.SHA256 && a.Size == b.Size && a.CommittedOffset == b.CommittedOffset
}
func (m *Manager) ackPath(r Receipt) string {
	return filepath.Join(m.root, "acknowledgements", digest(r.SourceID), r.SegmentUUID+".json")
}
func verifyArchive(path string, r Receipt) error {
	f, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
	if err != nil {
		return err
	}
	defer f.Close()
	st, err := f.Stat()
	if err != nil || !st.Mode().IsRegular() || st.Size() != r.Size {
		return errors.New("archive_mismatch")
	}
	h := sha256.New()
	if _, err = io.Copy(h, f); err != nil {
		return err
	}
	if hex.EncodeToString(h.Sum(nil)) != r.SHA256 {
		return errors.New("archive_mismatch")
	}
	return nil
}

// Cleanup requires a matching fixture ack, retained raw archive and elapsed
// source window. An intent is durable before unlink; a completion follows it.
func (m *Manager) Cleanup(path string) error {
	r, st, err := m.sealed(path)
	if err != nil {
		return err
	}
	b, err := os.ReadFile(m.ackPath(r))
	if err != nil {
		return errors.New("ack_missing")
	}
	var ack Receipt
	if json.Unmarshal(b, &ack) != nil || !same(ack, r) || ack.Kind != "fixture_ack" || ack.RawLocation != filepath.Join(m.root, "fixture-raw", digest(r.SourceID), r.SegmentUUID+".jsonl") || ack.AcknowledgedAt.IsZero() || ack.AcknowledgedAt.After(m.now()) {
		return errors.New("ack_mismatch")
	}
	if m.now().Sub(st.ModTime()) < m.policy.SourceRetention || m.now().Sub(ack.AcknowledgedAt) < m.policy.SourceRetention {
		return errors.New("retention_active")
	}
	if m.now().Sub(ack.AcknowledgedAt) > m.policy.EvidenceRetention {
		return errors.New("evidence_expired")
	}
	if err = verifyArchive(ack.RawLocation, r); err != nil {
		return err
	}
	base := filepath.Join(m.root, "cleanup", digest(r.SourceID), r.SegmentUUID)
	intent, _ := json.Marshal(struct {
		Receipt    Receipt `json:"receipt"`
		SealedPath string  `json:"sealed_path"`
	}{ack, path})
	intent = append(intent, '\n')
	if err = publish(base+".intent.json", intent); err != nil {
		return err
	}
	// Recheck that the pathname still refers to the same sealed bytes.
	again, againSt, err := m.sealed(path)
	if err != nil || !same(again, r) || !os.SameFile(st, againSt) {
		return errors.New("segment_changed")
	}
	if err = os.Remove(path); err != nil {
		return err
	}
	if err = syncDir(filepath.Dir(path)); err != nil {
		return err
	}
	complete := []byte(fmt.Sprintf("{\"segment_uuid\":%q,\"sha256\":%q,\"completed_at\":%q}\n", r.SegmentUUID, r.SHA256, m.now().UTC().Format(time.RFC3339Nano)))
	return publish(base+".complete.json", complete)
}
