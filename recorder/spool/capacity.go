package spool

import (
	"errors"
	"io/fs"
	"math"
	"os"
	"path/filepath"
	"syscall"
)

// lockCapacity serializes admission and appends by cooperating producers. The
// target shared filesystem must qualify flock before these limits are deployed.
func lockCapacity(root string) (func(), error) {
	dir := filepath.Join(root, "spool")
	if err := ensureDir(dir); err != nil {
		return nil, err
	}
	f, err := os.OpenFile(filepath.Join(dir, ".capacity.lock"), os.O_CREATE|os.O_RDWR|syscall.O_NOFOLLOW, 0600)
	if err != nil {
		return nil, bad("spool_unavailable")
	}
	st, err := f.Stat()
	if err != nil || !st.Mode().IsRegular() || st.Mode().Perm() != 0600 {
		f.Close()
		return nil, bad("spool_unavailable")
	}
	if err := syscall.Flock(int(f.Fd()), syscall.LOCK_EX|syscall.LOCK_NB); err != nil {
		f.Close()
		if errors.Is(err, syscall.EWOULDBLOCK) {
			return nil, bad("writer_busy")
		}
		return nil, bad("spool_unavailable")
	}
	return func() { _ = syscall.Flock(int(f.Fd()), syscall.LOCK_UN); _ = f.Close() }, nil
}

func spoolUsage(root string) (int64, int64, error) {
	var bytes, files int64
	err := filepath.WalkDir(filepath.Join(root, "spool"), func(path string, d fs.DirEntry, walkErr error) error {
		if walkErr != nil {
			return walkErr
		}
		st, err := os.Lstat(path)
		if err != nil {
			return err
		}
		if d.IsDir() {
			if st.Mode().Perm() != 0700 {
				return errors.New("unsafe spool directory")
			}
			return nil
		}
		if !st.Mode().IsRegular() || st.Mode().Perm() != 0600 {
			return errors.New("unsafe spool entry")
		}
		if st.Size() > math.MaxInt64-bytes || files == math.MaxInt64 {
			return errors.New("spool usage overflow")
		}
		bytes += st.Size()
		files++
		return nil
	})
	if err != nil {
		return 0, 0, bad("spool_unavailable")
	}
	return bytes, files, nil
}

func exceeds(used, reserve, limit int64) bool { return used > limit || reserve > limit-used }
