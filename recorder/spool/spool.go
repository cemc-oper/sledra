// Package spool writes bounded, per-session JSONL segments on a qualified filesystem.
package spool

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
	"sync"
	"syscall"
	"time"

	"github.com/cemc-oper/sledra/recorder/event"
)

type Error struct{ Code string }

func (e *Error) Error() string { return e.Code }
func bad(code string) error    { return &Error{Code: code} }

// Limits are deployment settings. Zero values select synthetic local defaults,
// not a qualified HPC capacity or latency budget.
type Limits struct {
	SegmentBytes  int64
	SessionBytes  int64
	Segments      int
	SegmentAge    time.Duration
	MaxSpoolBytes int64
	MaxSpoolFiles int64
}

func (l Limits) normalized() (Limits, error) {
	if l.SegmentBytes == 0 {
		l.SegmentBytes = 1 << 20
	}
	if l.SessionBytes == 0 {
		l.SessionBytes = 64 << 20
	}
	if l.Segments == 0 {
		l.Segments = 64
	}
	if l.SegmentAge == 0 {
		l.SegmentAge = 5 * time.Minute
	}
	if l.SegmentBytes < event.MaxEventBytes+512 || l.SessionBytes < l.SegmentBytes || l.Segments < 1 || l.SegmentAge < 0 || l.MaxSpoolBytes < 0 || l.MaxSpoolFiles < 0 || (l.MaxSpoolBytes == 0) != (l.MaxSpoolFiles == 0) {
		return Limits{}, bad("config_invalid")
	}
	return l, nil
}

type Stats struct {
	Written, Dropped, Bytes, Segments int64
	LastError                         string
}
type Writer struct {
	mu                 sync.Mutex
	root               string
	dir                string
	source, session    string
	run, task, attempt string
	limits             Limits
	codec              *event.Codec
	lock               *os.File
	file               *os.File
	fileName           string
	created            time.Time
	stats              Stats
	poisoned           bool
	write              func(*os.File, []byte) (int, error)
}

// Open obtains a nonblocking per-session advisory lock. Cross-process session
// reuse is allowed only where this lock and same-directory publication have
// been qualified; SLD-03 currently qualifies local WSL/ext4 only.
func Open(root, role, source, run, task, attempt, session string, limits Limits) (*Writer, error) {
	l, err := limits.normalized()
	if err != nil {
		return nil, err
	}
	if root == "" || role == "" || source == "" || run == "" || session == "" || len(source) > 128 || len(session) > 128 {
		return nil, bad("config_invalid")
	}
	codec, err := event.New(role)
	if err != nil {
		return nil, bad("contract_unavailable")
	}
	root, err = filepath.Abs(root)
	if err != nil {
		return nil, bad("spool_unavailable")
	}
	if err = safeRoot(root); err != nil {
		return nil, err
	}
	if l.MaxSpoolBytes > 0 {
		unlock, err := lockCapacity(root)
		if err != nil {
			return nil, err
		}
		defer unlock()
		bytes, files, err := spoolUsage(root)
		if err != nil {
			return nil, err
		}
		// Reserve a session lock and one new segment before creating paths.
		if exceeds(bytes, int64(event.MaxEventBytes+512), l.MaxSpoolBytes) || exceeds(files, 2, l.MaxSpoolFiles) {
			return nil, bad("spool_full")
		}
	}
	parts := []string{"spool", digest(source), digest(run), digest(task), digest(attempt)}
	dir := root
	for _, part := range parts {
		dir = filepath.Join(dir, part)
		if err = ensureDir(dir); err != nil {
			return nil, err
		}
	}
	lock, err := os.OpenFile(filepath.Join(dir, digest(session)+".lock"), os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0600)
	if err != nil {
		return nil, bad("spool_unavailable")
	}
	lockStat, err := lock.Stat()
	if err != nil || !lockStat.Mode().IsRegular() || lockStat.Mode().Perm() != 0600 {
		lock.Close()
		return nil, bad("spool_unavailable")
	}
	if err = syscall.Flock(int(lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		lock.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, bad("writer_busy")
		}
		return nil, bad("spool_unavailable")
	}
	w := &Writer{root: root, dir: dir, source: source, session: session, run: run, task: task, attempt: attempt, limits: l, codec: codec, lock: lock, write: func(f *os.File, b []byte) (int, error) { return f.Write(b) }}
	if err = w.resume(); err != nil {
		w.Close()
		return nil, err
	}
	return w, nil
}

func digest(s string) string { h := sha256.Sum256([]byte(s)); return hex.EncodeToString(h[:]) }
func safeRoot(root string) error {
	st, err := os.Lstat(root)
	if err != nil || !st.IsDir() {
		return bad("spool_unavailable")
	}
	real, err := filepath.EvalSymlinks(root)
	if err != nil || real != root {
		return bad("spool_unavailable")
	}
	return nil
}
func ensureDir(path string) error {
	if err := os.Mkdir(path, 0700); err != nil && !errors.Is(err, os.ErrExist) {
		return bad("spool_unavailable")
	}
	st, err := os.Lstat(path)
	if err != nil || !st.IsDir() || st.Mode()&os.ModeSymlink != 0 || st.Mode().Perm() != 0700 {
		return bad("spool_unavailable")
	}
	return nil
}

func (w *Writer) resume() error {
	entries, err := os.ReadDir(w.dir)
	if err != nil {
		return bad("spool_unavailable")
	}
	prefix := digest(w.session) + "-"
	var names []string
	for _, entry := range entries {
		if strings.HasPrefix(entry.Name(), prefix) && strings.HasSuffix(entry.Name(), ".open.jsonl") {
			names = append(names, entry.Name())
		}
	}
	// Select only the most recently touched open segment. An older complete
	// segment must not be resumed past a newer partial tail.
	var latest string
	var latestTime time.Time
	for _, name := range names {
		st, err := os.Lstat(filepath.Join(w.dir, name))
		if err != nil || !st.Mode().IsRegular() || st.Mode().Perm() != 0600 {
			return bad("spool_unavailable")
		}
		if latest == "" || st.ModTime().After(latestTime) {
			latest, latestTime = name, st.ModTime()
		}
	}
	if latest == "" {
		return nil
	}
	for _, name := range []string{latest} {
		path := filepath.Join(w.dir, name)
		st, err := os.Lstat(path)
		if err != nil || !st.Mode().IsRegular() || st.Mode().Perm() != 0600 {
			return bad("spool_unavailable")
		}
		if st.Size() == 0 || st.Size() >= w.limits.SegmentBytes {
			continue
		}
		f, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND|syscall.O_NOFOLLOW, 0600)
		if err != nil {
			return bad("spool_unavailable")
		}
		if st.Size() > 0 {
			r, err := os.OpenFile(path, os.O_RDONLY|syscall.O_NOFOLLOW, 0)
			if err != nil {
				f.Close()
				return bad("spool_unavailable")
			}
			headerLine, headerErr := bufio.NewReader(io.LimitReader(r, 4096)).ReadBytes('\n')
			var header struct {
				RecordKind      string `json:"record_kind"`
				ProtocolVersion string `json:"protocol_version"`
				SourceID        string `json:"source_id"`
				SegmentUUID     string `json:"segment_uuid"`
				WriterSession   string `json:"writer_session"`
				CreatedAt       string `json:"created_at"`
			}
			if headerErr != nil || json.Unmarshal(headerLine, &header) != nil || header.RecordKind != "segment_header" || header.ProtocolVersion != "1.0.0" || header.SourceID != w.source || header.WriterSession != w.session || name != digest(w.session)+"-"+header.SegmentUUID+".open.jsonl" {
				r.Close()
				f.Close()
				return bad("spool_unavailable")
			}
			created, parseErr := time.Parse(time.RFC3339Nano, header.CreatedAt)
			if parseErr != nil {
				r.Close()
				f.Close()
				return bad("spool_unavailable")
			}
			if time.Since(created) >= w.limits.SegmentAge {
				r.Close()
				f.Close()
				continue
			}
			_, err = r.Seek(-1, io.SeekEnd)
			var tail [1]byte
			if err == nil {
				_, err = io.ReadFull(r, tail[:])
			}
			r.Close()
			if err != nil || tail[0] != '\n' {
				f.Close()
				continue
			}
			w.created = created
		}
		w.file, w.fileName = f, name
		return nil
	}
	return nil
}

func (w *Writer) usage() (int64, int, error) {
	entries, err := os.ReadDir(w.dir)
	if err != nil {
		return 0, 0, bad("spool_unavailable")
	}
	prefix := digest(w.session) + "-"
	var bytes int64
	var count int
	for _, entry := range entries {
		name := entry.Name()
		if !strings.HasPrefix(name, prefix) || (!strings.HasSuffix(name, ".open.jsonl") && !strings.HasSuffix(name, ".sealed.jsonl")) {
			continue
		}
		st, err := os.Lstat(filepath.Join(w.dir, name))
		if err != nil || !st.Mode().IsRegular() {
			return 0, 0, bad("spool_unavailable")
		}
		bytes += st.Size()
		count++
	}
	return bytes, count, nil
}

// Append validates and fully encodes before opening or modifying a segment.
// A failed/short append poisons this writer so the partial open tail remains
// visible and cannot be followed by a new line in the same segment.
func (w *Writer) Append(input []byte) error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.lock == nil || w.poisoned {
		return bad("spool_unavailable")
	}
	result, err := w.codec.Validate(input)
	if err != nil {
		return err
	}
	var e struct {
		Source struct {
			SourceID  string `json:"source_id"`
			SessionID string `json:"session_id"`
		} `json:"source"`
		Association struct {
			RunID          string `json:"run_id"`
			TaskInstanceID string `json:"task_instance_id"`
			AttemptID      string `json:"attempt_id"`
		} `json:"association"`
	}
	if json.Unmarshal(result.Bytes, &e) != nil || e.Source.SourceID != w.source || e.Source.SessionID != w.session {
		return bad("parent_mismatch")
	}
	if e.Association.RunID != w.run || e.Association.TaskInstanceID != w.task || e.Association.AttemptID != w.attempt {
		return bad("parent_mismatch")
	}
	if w.limits.MaxSpoolBytes > 0 {
		unlock, err := lockCapacity(w.root)
		if err != nil {
			return err
		}
		defer unlock()
		bytes, files, err := spoolUsage(w.root)
		if err != nil {
			return err
		}
		reserve := int64(len(result.Bytes))
		newFile := int64(0)
		if w.file == nil {
			reserve += 512
			newFile = 1
		} else if st, e := w.file.Stat(); e != nil {
			return bad("spool_unavailable")
		} else if st.Size()+int64(len(result.Bytes)) > w.limits.SegmentBytes || time.Since(w.created) >= w.limits.SegmentAge {
			reserve += 512
			newFile = 1
		}
		if exceeds(bytes, reserve, w.limits.MaxSpoolBytes) || exceeds(files, newFile, w.limits.MaxSpoolFiles) {
			return bad("spool_full")
		}
	}
	bytes, count, err := w.usage()
	if err != nil {
		return err
	}
	if bytes+int64(len(result.Bytes))+512 > w.limits.SessionBytes {
		return bad("spool_full")
	}
	if w.file != nil {
		st, err := w.file.Stat()
		if err != nil {
			return bad("spool_unavailable")
		}
		if st.Size()+int64(len(result.Bytes)) > w.limits.SegmentBytes || time.Since(w.created) >= w.limits.SegmentAge {
			if err := w.seal(); err != nil {
				return err
			}
		}
	}
	if w.file == nil {
		if count >= w.limits.Segments {
			return bad("spool_full")
		}
		if err := w.create(); err != nil {
			return err
		}
		bytes, _, err = w.usage()
		if err != nil {
			return err
		}
		if bytes+int64(len(result.Bytes)) > w.limits.SessionBytes {
			return bad("spool_full")
		}
	}
	n, err := w.write(w.file, result.Bytes)
	if err != nil || n != len(result.Bytes) {
		w.poisoned = true
		return bad("spool_unavailable")
	}
	w.stats.Written++
	w.stats.Bytes += int64(n)
	return nil
}

func (w *Writer) create() error {
	id, err := event.NewID()
	if err != nil {
		return bad("spool_unavailable")
	}
	name := digest(w.session) + "-" + id + ".open.jsonl"
	header, _ := json.Marshal(struct {
		RecordKind      string `json:"record_kind"`
		ProtocolVersion string `json:"protocol_version"`
		SourceID        string `json:"source_id"`
		SegmentUUID     string `json:"segment_uuid"`
		WriterSession   string `json:"writer_session"`
		CreatedAt       string `json:"created_at"`
	}{"segment_header", "1.0.0", w.source, id, w.session, time.Now().UTC().Format(time.RFC3339Nano)})
	header = append(header, '\n')
	if int64(len(header))+event.MaxEventBytes > w.limits.SegmentBytes {
		return bad("config_invalid")
	}
	tmp, err := os.CreateTemp(w.dir, ".segment-*")
	if err != nil {
		return bad("spool_unavailable")
	}
	defer os.Remove(tmp.Name())
	if _, err = tmp.Write(header); err == nil {
		err = tmp.Sync()
	}
	if err == nil {
		err = tmp.Close()
	} else {
		tmp.Close()
	}
	if err != nil {
		return bad("spool_unavailable")
	}
	path := filepath.Join(w.dir, name)
	if err = os.Link(tmp.Name(), path); err != nil {
		return bad("spool_unavailable")
	}
	if err = os.Remove(tmp.Name()); err != nil {
		return bad("spool_unavailable")
	}
	if err = syncDir(w.dir); err != nil {
		return err
	}
	f, err := os.OpenFile(path, os.O_WRONLY|os.O_APPEND|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return bad("spool_unavailable")
	}
	w.file, w.fileName, w.created = f, name, time.Now()
	w.stats.Segments++
	return nil
}
func syncDir(dir string) error {
	f, err := os.Open(dir)
	if err != nil {
		return bad("spool_unavailable")
	}
	defer f.Close()
	if f.Sync() != nil {
		return bad("spool_unavailable")
	}
	return nil
}
func (w *Writer) seal() error {
	if w.file == nil {
		return nil
	}
	if err := w.file.Sync(); err != nil {
		w.poisoned = true
		return bad("spool_unavailable")
	}
	if err := w.file.Close(); err != nil {
		w.poisoned = true
		return bad("spool_unavailable")
	}
	w.file = nil
	open := filepath.Join(w.dir, w.fileName)
	sealed := strings.TrimSuffix(open, ".open.jsonl") + ".sealed.jsonl"
	if err := os.Link(open, sealed); err != nil {
		w.poisoned = true
		return bad("spool_unavailable")
	}
	if err := os.Remove(open); err != nil {
		w.poisoned = true
		return bad("spool_unavailable")
	}
	if err := syncDir(w.dir); err != nil {
		w.poisoned = true
		return err
	}
	w.fileName = ""
	return nil
}
func (w *Writer) Seal() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.poisoned {
		return bad("spool_unavailable")
	}
	return w.seal()
}
func (w *Writer) Close() error {
	w.mu.Lock()
	defer w.mu.Unlock()
	var err error
	if w.file != nil {
		if e := w.file.Sync(); e != nil {
			err = bad("spool_unavailable")
		}
		if e := w.file.Close(); e != nil {
			err = bad("spool_unavailable")
		}
		w.file = nil
	}
	if w.lock != nil {
		_ = syscall.Flock(int(w.lock.Fd()), syscall.LOCK_UN)
		_ = w.lock.Close()
		w.lock = nil
	}
	return err
}
func (w *Writer) Stats() Stats { w.mu.Lock(); defer w.mu.Unlock(); return w.stats }

// Record applies the business-mode policy and keeps only classified counters.
func (w *Writer) Record(input []byte, strict bool) error {
	err := w.Append(input)
	if err == nil {
		return nil
	}
	w.mu.Lock()
	w.stats.Dropped++
	w.stats.LastError = Code(err)
	w.mu.Unlock()
	if strict {
		return err
	}
	return nil
}
func Code(err error) string {
	if err == nil {
		return ""
	}
	var e *Error
	if errors.As(err, &e) {
		return e.Code
	}
	var ve *event.Error
	if errors.As(err, &ve) {
		return ve.Code
	}
	switch err.Error() {
	case "root_missing", "config_invalid", "input_unavailable", "contract_unavailable", "contract_incompatible", "contract_path_invalid", "schema_invalid", "context_missing", "context_invalid", "parent_mismatch", "limit_exceeded", "spool_full", "writer_busy":
		return err.Error()
	}
	return "spool_unavailable"
}
func (w *Writer) String() string {
	return fmt.Sprintf("session=%s written=%d dropped=%d", digest(w.session), w.Stats().Written, w.Stats().Dropped)
}
