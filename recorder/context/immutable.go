package context

import (
	"bytes"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
)

const maxDocumentBytes = 4 << 20

func inside(root, path string) (string, error) {
	if !filepath.IsAbs(path) {
		return "", fail("path_invalid")
	}
	clean := filepath.Clean(path)
	rel, err := filepath.Rel(root, clean)
	if err != nil || rel == "." || rel == ".." || strings.HasPrefix(rel, ".."+string(os.PathSeparator)) {
		return "", fail("path_invalid")
	}
	return clean, nil
}

func realPath(root, path string, create bool) error {
	clean, err := inside(root, path)
	if err != nil {
		return err
	}
	rel, _ := filepath.Rel(root, filepath.Dir(clean))
	parent := root
	if rel != "." {
		for _, part := range strings.Split(rel, string(os.PathSeparator)) {
			child := filepath.Join(parent, part)
			if create {
				if err := os.Mkdir(child, 0700); err != nil && !errors.Is(err, os.ErrExist) {
					return fail("path_unavailable")
				}
			}
			info, err := os.Lstat(child)
			if !create && errors.Is(err, os.ErrNotExist) {
				return fail("context_missing")
			}
			if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
				return fail("path_unavailable")
			}
			if create {
				if err := syncDirectory(parent); err != nil {
					return fail("indeterminate")
				}
			}
			parent = child
		}
	}
	return nil
}

func syncDirectory(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	return f.Sync()
}

func readBounded(path string) ([]byte, error) {
	info, err := os.Lstat(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, fail("context_missing")
	}
	if err != nil || !info.Mode().IsRegular() || info.Size() > maxDocumentBytes {
		return nil, fail("context_invalid")
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, fail("context_missing")
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, maxDocumentBytes+1))
	if err != nil || len(b) > maxDocumentBytes {
		return nil, fail("context_invalid")
	}
	return b, nil
}

// publish returns the exact existing winner on a race or duplicate. The
// caller compares its semantic request before considering that a success.
func publish(root, path string, data []byte) ([]byte, bool, error) {
	if len(data) > maxDocumentBytes {
		return nil, false, fail("limit_exceeded")
	}
	if err := realPath(root, path, true); err != nil {
		return nil, false, err
	}
	if winner, err := readBounded(path); err == nil {
		return winner, false, nil
	} else if !isCode(err, "context_missing") {
		return nil, false, err
	}
	dir := filepath.Dir(path)
	tmp, err := os.CreateTemp(dir, ".sledra-tmp-")
	if err != nil {
		return nil, false, fail("path_unavailable")
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return nil, false, fail("path_unavailable")
	}
	if err := tmp.Chmod(0400); err != nil {
		tmp.Close()
		return nil, false, fail("path_unavailable")
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return nil, false, fail("path_unavailable")
	}
	if err := tmp.Close(); err != nil {
		return nil, false, fail("path_unavailable")
	}
	if err := os.Link(tmp.Name(), path); err != nil {
		if !errors.Is(err, os.ErrExist) {
			return nil, false, fail("indeterminate")
		}
		if err := syncDirectory(dir); err != nil {
			return nil, false, fail("indeterminate")
		}
		winner, err := readBounded(path)
		return winner, false, err
	}
	if err := syncDirectory(dir); err != nil {
		return nil, false, fail("indeterminate")
	}
	if err := os.Remove(tmp.Name()); err != nil {
		return nil, false, fail("indeterminate")
	}
	if err := syncDirectory(dir); err != nil {
		return nil, false, fail("indeterminate")
	}
	return data, true, nil
}

func publishEqual(root, path string, data []byte) error {
	winner, _, err := publish(root, path, data)
	if err != nil {
		return err
	}
	if !bytes.Equal(winner, data) {
		return fail("conflict")
	}
	return nil
}
