//go:build linux

package execution

import (
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"syscall"
	"unsafe"
)

func ChildSupported() bool { return runtime.GOARCH == "amd64" || runtime.GOARCH == "arm64" }

type childOwner struct {
	UID                    uint32
	PID                    int
	Start, Boot, Namespace string
}

func childProc(pid int) (start, state string, ppid int, err error) {
	raw, e := os.ReadFile(fmt.Sprintf("/proc/%d/stat", pid))
	if e != nil || len(raw) > 16384 {
		err = bad("owner_gone")
		return
	}
	end := strings.LastIndexByte(string(raw), ')')
	if end < 0 {
		err = bad("owner_gone")
		return
	}
	fields := strings.Fields(string(raw[end+1:]))
	if len(fields) < 20 {
		err = bad("owner_gone")
		return
	}
	state = fields[0]
	ppid, e = strconv.Atoi(fields[1])
	if e != nil || state == "Z" || state == "X" {
		err = bad("owner_gone")
		return
	}
	start = fields[19]
	if childUint(start, true) != nil {
		err = bad("owner_gone")
	}
	return
}
func childIdentity(pid int, ancestor bool) (childOwner, error) {
	owner := childOwner{PID: pid}
	if pid <= 0 {
		return owner, bad("identity_mismatch")
	}
	start, _, _, err := childProc(pid)
	if err != nil {
		return owner, err
	}
	owner.Start = start
	info, err := os.Stat(fmt.Sprintf("/proc/%d", pid))
	if err != nil {
		return owner, bad("owner_gone")
	}
	owner.UID = info.Sys().(*syscall.Stat_t).Uid
	if owner.UID != uint32(os.Geteuid()) {
		return owner, bad("identity_mismatch")
	}
	exe, err := os.Readlink(fmt.Sprintf("/proc/%d/exe", pid))
	if err != nil || filepath.Base(exe) != "bash" {
		return owner, bad("owner_gone")
	}
	boot, err := os.ReadFile("/proc/sys/kernel/random/boot_id")
	if err != nil || len(boot) > 128 {
		return owner, bad("identity_mismatch")
	}
	owner.Boot = strings.TrimSpace(string(boot))
	ns, err := os.Stat(fmt.Sprintf("/proc/%d/ns/pid", pid))
	if err != nil {
		return owner, bad("identity_mismatch")
	}
	ownNS, err := os.Stat("/proc/self/ns/pid")
	if err != nil || !os.SameFile(ns, ownNS) {
		return owner, bad("identity_mismatch")
	}
	owner.Namespace = strconv.FormatUint(ns.Sys().(*syscall.Stat_t).Ino, 10)
	if ancestor {
		found := false
		for current, steps := os.Getppid(), 0; current > 0 && steps < 1024; steps++ {
			if current == pid {
				found = true
				break
			}
			_, _, parent, err := childProc(current)
			if err != nil || parent == current {
				break
			}
			current = parent
		}
		if !found {
			return owner, bad("role_denied")
		}
	}
	return owner, nil
}

func childFileID(info os.FileInfo) (device, inode string) {
	s := info.Sys().(*syscall.Stat_t)
	return strconv.FormatUint(uint64(s.Dev), 10), strconv.FormatUint(s.Ino, 10)
}
func childMatches(info os.FileInfo, device, inode string) bool {
	d, i := childFileID(info)
	return d == device && i == inode
}
func childPrivate(info os.FileInfo, dir bool) bool {
	if info == nil {
		return false
	}
	s := info.Sys().(*syscall.Stat_t)
	if s.Uid != uint32(os.Geteuid()) {
		return false
	}
	if dir {
		return info.IsDir() && info.Mode().Perm() == 0700
	}
	return info.Mode().IsRegular() && info.Mode().Perm() == 0600 && s.Nlink == 1
}

// Each component is opened relative to a pinned directory and checked against its inode.
// World/group-writable ancestors (including /tmp) are intentionally unsupported.
func childDirectory(path string, private bool) (*os.Root, error) {
	if childAbsolute(path, 2048) != nil {
		return nil, bad("invalid_reference")
	}
	dir, err := os.OpenRoot("/")
	if err != nil {
		return nil, bad("invalid_reference")
	}
	for _, part := range strings.Split(strings.TrimPrefix(path, "/"), "/") {
		if part == "" {
			continue
		}
		before, e := dir.Lstat(part)
		if e != nil || !before.IsDir() || before.Mode().Perm()&0022 != 0 {
			dir.Close()
			return nil, bad("invalid_reference")
		}
		next, e := dir.OpenRoot(part)
		if e != nil {
			dir.Close()
			return nil, bad("invalid_reference")
		}
		actual, e := next.Stat(".")
		dir.Close()
		if e != nil || !os.SameFile(before, actual) {
			next.Close()
			return nil, bad("identity_mismatch")
		}
		dir = next
	}
	info, err := dir.Stat(".")
	if err != nil || private && !childPrivate(info, true) {
		dir.Close()
		return nil, bad("invalid_reference")
	}
	return dir, nil
}
func childFile(dir *os.Root, name string, flags int) (*os.File, error) {
	f, err := dir.OpenFile(name, flags|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0600)
	if err != nil {
		return nil, err
	}
	info, err := f.Stat()
	path, pathErr := dir.Lstat(name)
	valid := err == nil && childPrivate(info, false)
	if err == nil && name == "state.json" && flags == os.O_RDONLY {
		// A complete old state snapshot may be unlinked by an atomic rename
		// after open. Its immutable session/key binding is checked by the caller.
		stat := info.Sys().(*syscall.Stat_t)
		valid = info.Mode().IsRegular() && info.Mode().Perm() == 0600 && stat.Uid == uint32(os.Geteuid()) && stat.Nlink <= 1
	}
	if err != nil || pathErr != nil || !valid || !childPrivate(path, false) || (name != "state.json" && !os.SameFile(info, path)) {
		f.Close()
		return nil, bad("control_unavailable")
	}
	return f, nil
}
func childRead(dir *os.Root, name string, limit int) ([]byte, error) {
	f, err := childFile(dir, name, os.O_RDONLY)
	if err != nil {
		return nil, err
	}
	raw, readErr := io.ReadAll(io.LimitReader(f, int64(limit+1)))
	closeErr := f.Close()
	if readErr != nil || closeErr != nil {
		return nil, bad("control_unavailable")
	}
	if len(raw) > limit {
		return nil, bad("limit_exceeded")
	}
	return raw, nil
}
func childLatch(dir *os.Root, name string) (bool, error) {
	raw, err := childRead(dir, name, 0)
	if errors.Is(err, os.ErrNotExist) {
		return false, nil
	}
	if err != nil || len(raw) != 0 {
		return false, bad("control_unavailable")
	}
	return true, nil
}
func childPublishLatch(dir *os.Root, name string) error {
	f, err := childFile(dir, name, os.O_WRONLY|os.O_CREATE|os.O_EXCL)
	if errors.Is(err, os.ErrExist) {
		_, err = childRead(dir, name, 0)
		return err
	}
	if err != nil {
		return err
	}
	return f.Close()
}
func childRenameNoReplace(dir *os.Root, old, new string) error {
	// Linux syscall numbers differ between the two supported trial architectures.
	var number uintptr
	switch runtime.GOARCH {
	case "amd64":
		number = 316
	case "arm64":
		number = 276
	default:
		return syscall.ENOSYS
	}
	fd, err := dir.Open(".")
	if err != nil {
		return err
	}
	defer fd.Close()
	from, err := syscall.BytePtrFromString(old)
	if err != nil {
		return err
	}
	to, err := syscall.BytePtrFromString(new)
	if err != nil {
		return err
	}
	_, _, errno := syscall.Syscall6(number, fd.Fd(), uintptr(unsafe.Pointer(from)), fd.Fd(), uintptr(unsafe.Pointer(to)), 1, 0)
	runtime.KeepAlive(from)
	runtime.KeepAlive(to)
	if errno != 0 {
		return errno
	}
	return nil
}

// atomic publishes complete files only; hooks are test-local, never environment switches.
func (r *ChildRecorder) atomic(dir *os.Root, name string, raw []byte, exclusive bool, point string) (err error) {
	if err = r.cut(point + ".before-write"); err != nil {
		return
	}
	tmp := "." + name + ".tmp"
	f, err := childFile(dir, tmp, os.O_WRONLY|os.O_CREATE|os.O_EXCL)
	if err != nil {
		return
	}
	renamed := false
	defer func() {
		if !renamed {
			if e := dir.Remove(tmp); e != nil && err == nil {
				err = e
			}
		}
	}()
	n, writeErr := f.Write(raw)
	cutErr := r.cut(point + ".after-write")
	closeErr := f.Close()
	closeCut := r.cut(point + ".close")
	if writeErr != nil || n != len(raw) || cutErr != nil || closeErr != nil || closeCut != nil {
		return bad("control_unavailable")
	}
	if err = r.cut(point + ".before-rename"); err != nil {
		return
	}
	if exclusive {
		err = childRenameNoReplace(dir, tmp, name)
	} else {
		err = dir.Rename(tmp, name)
	}
	if err != nil {
		return
	}
	renamed = true
	return r.cut(point + ".after-rename")
}
