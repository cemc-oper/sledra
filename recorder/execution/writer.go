package execution

import (
	"bytes"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
)

// Writer supports one sequential owner. It neither launches work nor retries I/O.
// A future Shell owner must retain disabled state across separate CLI processes.
type Writer struct {
	file      *os.File
	codec     *Codec
	id        string
	path      string
	write     func([]byte) (int, error)
	closeFile func() error
	disabled  bool
	closed    bool
	closeErr  error
	fresh     bool
}

func (w *Writer) Path() string { return w.path }

// Create writes an automatic/prepared start into an exclusively created file.
func (r *Root) Create(start []byte) (*Writer, error) {
	event, err := r.codec.Validate("event", start)
	if err != nil {
		return nil, err
	}
	if event["name"] != "execution.started" {
		return nil, bad("schema_invalid")
	}
	id, entry := event["execution_id"].(string), event["occurred_at"].(string)
	path, err := ExecutionPath(id, entry, r.config)
	if err != nil {
		return nil, err
	}
	if err := r.parent(path, true); err != nil {
		return nil, err
	}
	w, err := r.openWriter(id, path, os.O_CREATE|os.O_EXCL|os.O_RDWR|os.O_APPEND)
	if err != nil {
		return nil, err
	}
	w.fresh = true
	if err := w.Append(start); err != nil {
		_ = w.Close()
		return nil, err
	}
	return w, nil
}

func (r *Root) parent(path string, create bool) error {
	// Only three fixed levels, checked without a root walk or lock service.
	parent := "."
	for _, part := range strings.Split(filepath.Dir(path), string(filepath.Separator)) {
		parent = filepath.Join(parent, part)
		if create {
			if err := r.dir.Mkdir(parent, 0o700); err != nil && !errors.Is(err, os.ErrExist) {
				return bad("io_unavailable")
			}
		}
		info, err := r.dir.Lstat(parent)
		if err != nil || !info.IsDir() {
			return bad("io_unavailable")
		}
	}
	return nil
}

// OpenWriter never creates/rebuilds a missing file, or repairs a partial tail.
// It does not recover disabled owner state; that state belongs to the caller.
func (r *Root) OpenWriter(id, entryTime string) (*Writer, error) {
	path, err := ExecutionPath(id, entryTime, r.config)
	if err != nil {
		return nil, err
	}
	if err := r.parent(path, false); err != nil {
		return nil, err
	}
	w, err := r.openWriter(id, path, os.O_RDWR|os.O_APPEND)
	if err != nil {
		return nil, err
	}
	if err := w.tail(); err != nil {
		_ = w.Close()
		return nil, err
	}
	return w, nil
}

func (r *Root) openWriter(id, path string, flags int) (*Writer, error) {
	f, err := regularFile(r.dir, path, flags)
	if err != nil {
		return nil, err
	}
	return &Writer{file: f, codec: r.codec, id: id, path: path, write: f.Write, closeFile: f.Close}, nil
}

// Append validates completely before I/O. Raw must exclude the terminal LF.
func (w *Writer) Append(raw []byte) error {
	if w.disabled || w.closed {
		return bad("writer_disabled")
	}
	event, err := w.codec.Validate("event", raw)
	if err != nil {
		return err
	}
	if event["execution_id"] != w.id {
		return bad("execution_mismatch")
	}
	if !w.fresh {
		if err := w.tail(); err != nil {
			w.disabled = true
			return err
		}
	}
	w.fresh = false
	line := append(bytes.Clone(raw), '\n')
	n, err := w.write(line)
	if err != nil || n != len(line) {
		w.disabled = true
		return bad("indeterminate")
	}
	return nil
}

func (w *Writer) tail() error {
	info, err := w.file.Stat()
	if err != nil {
		return bad("io_unavailable")
	}
	if info.Size() == 0 {
		return bad("incomplete_tail")
	}
	var last [1]byte
	if _, err := w.file.ReadAt(last[:], info.Size()-1); err != nil {
		return bad("io_unavailable")
	}
	if last[0] != '\n' {
		return bad("incomplete_tail")
	}
	return nil
}

func (w *Writer) Close() error {
	if w.closed {
		return w.closeErr
	}
	w.closed = true
	if err := w.closeFile(); err != nil {
		w.disabled = true
		w.closeErr = bad("indeterminate")
	}
	return w.closeErr
}

// ReadRecord bounds a single input object, never an entire history stream.
func ReadRecord(reader io.Reader) ([]byte, error) {
	raw, err := io.ReadAll(io.LimitReader(reader, MaxRecordBytes+1))
	if err != nil {
		return nil, bad("input_unavailable")
	}
	if len(raw) > MaxRecordBytes {
		return nil, bad("limit_exceeded")
	}
	return raw, nil
}
