package execution

import (
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"syscall"
	"time"
)

type Namespace struct {
	System     string `json:"system"`
	Deployment string `json:"deployment"`
}

type RootConfig struct {
	FormatVersion     string    `json:"format_version"`
	LayoutVersion     int       `json:"layout_version"`
	Namespace         Namespace `json:"namespace"`
	PartitionTimezone string    `json:"partition_timezone"`
}

// Root holds only a directory descriptor, validated deployment settings and codec.
type Root struct {
	dir    *os.Root
	config RootConfig
	codec  *Codec
}

func (r *Root) Config() RootConfig { return r.config }
func (r *Root) Close() error       { return r.dir.Close() }

var zonePattern = regexp.MustCompile(`^[+-][0-9]{2}:[0-9]{2}$`)

func partitionZone(value string) (*time.Location, error) {
	if !zonePattern.MatchString(value) || value == "-00:00" {
		return nil, bad("invalid_timezone")
	}
	hour, _ := strconv.Atoi(value[1:3])
	minute, _ := strconv.Atoi(value[4:6])
	if hour > 14 || minute > 59 || (hour == 14 && minute != 0) {
		return nil, bad("invalid_timezone")
	}
	offset := (hour*60 + minute) * 60
	if value[0] == '-' {
		offset = -offset
	}
	return time.FixedZone(value, offset), nil
}

func openDirectory(path string) (*os.Root, error) {
	if !filepath.IsAbs(path) {
		return nil, bad("root_invalid")
	}
	clean := filepath.Clean(path)
	real, err := filepath.EvalSymlinks(clean)
	if err != nil || real != clean {
		return nil, bad("root_unavailable")
	}
	dir, err := os.OpenRoot(clean)
	if err != nil {
		return nil, bad("root_unavailable")
	}
	return dir, nil
}

func noLegacy(dir *os.Root) error {
	for _, name := range []string{"registry", "documents", "contexts", "spool"} {
		if _, err := dir.Lstat(name); !errors.Is(err, os.ErrNotExist) {
			return bad("root_incompatible")
		}
	}
	return nil
}

func regularFile(dir *os.Root, name string, flags int) (*os.File, error) {
	f, err := dir.OpenFile(name, flags|syscall.O_NOFOLLOW|syscall.O_NONBLOCK, 0o600)
	if err != nil {
		return nil, bad("io_unavailable")
	}
	info, err := f.Stat()
	if err != nil || !info.Mode().IsRegular() || info.Sys().(*syscall.Stat_t).Nlink != 1 {
		_ = f.Close()
		return nil, bad("io_unavailable")
	}
	return f, nil
}

func readConfig(dir *os.Root, codec *Codec) (RootConfig, error) {
	var config RootConfig
	f, err := regularFile(dir, "sledra.json", os.O_RDONLY)
	if err != nil {
		return config, bad("root_unavailable")
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, MaxRecordBytes+1))
	if err != nil {
		return config, bad("root_unavailable")
	}
	if _, err := codec.Validate("root", raw); err != nil {
		return config, bad("root_incompatible")
	}
	if err := json.Unmarshal(raw, &config); err != nil {
		return config, bad("root_incompatible")
	}
	return config, nil
}

func OpenRoot(path string) (*Root, error) {
	codec, err := NewCodec()
	if err != nil {
		return nil, err
	}
	dir, err := openDirectory(path)
	if err != nil {
		return nil, err
	}
	config, err := readConfig(dir, codec)
	if err == nil {
		err = noLegacy(dir)
	}
	if err != nil {
		_ = dir.Close()
		return nil, err
	}
	return &Root{dir: dir, config: config, codec: codec}, nil
}

// InitRoot is deployment setup, not per-task registration. Publish without replace.
func InitRoot(path string, config RootConfig) error {
	codec, err := NewCodec()
	if err != nil {
		return err
	}
	raw, err := json.Marshal(config)
	if err != nil {
		return bad("schema_invalid")
	}
	if _, err := codec.Validate("root", raw); err != nil {
		return err
	}
	if !filepath.IsAbs(path) {
		return bad("root_invalid")
	}
	// Reject existing symlink ancestors before MkdirAll could create outside the
	// requested tree. Deployment initialization does not accept path indirection.
	for ancestor := filepath.Clean(path); ; ancestor = filepath.Dir(ancestor) {
		info, err := os.Lstat(ancestor)
		if errors.Is(err, os.ErrNotExist) {
			continue
		}
		real, resolveErr := filepath.EvalSymlinks(ancestor)
		if err != nil || resolveErr != nil || real != ancestor || !info.IsDir() {
			return bad("root_unavailable")
		}
		break
	}
	if err := os.MkdirAll(path, 0o700); err != nil {
		return bad("root_unavailable")
	}
	dir, err := openDirectory(path)
	if err != nil {
		return err
	}
	defer dir.Close()
	if err := noLegacy(dir); err != nil {
		return err
	}
	if _, err := dir.Lstat("sledra.json"); err == nil {
		old, err := readConfig(dir, codec)
		if err != nil || old != config {
			return bad("root_incompatible")
		}
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return bad("root_unavailable")
	}
	listing, err := dir.Open(".")
	if err != nil {
		return bad("root_unavailable")
	}
	_, readErr := listing.Readdirnames(1)
	_ = listing.Close()
	if !errors.Is(readErr, io.EOF) {
		return bad("root_incompatible")
	}
	id, err := NewID()
	if err != nil {
		return err
	}
	name := ".sledra-init-" + id
	f, err := dir.OpenFile(name, os.O_WRONLY|os.O_CREATE|os.O_EXCL, 0o600)
	if err != nil {
		return bad("root_unavailable")
	}
	defer dir.Remove(name)
	n, writeErr := f.Write(raw)
	if writeErr == nil && n == len(raw) {
		writeErr = f.Sync()
	}
	closeErr := f.Close()
	if writeErr != nil || n != len(raw) || closeErr != nil {
		return bad("indeterminate")
	}
	if err := dir.Link(name, "sledra.json"); err != nil {
		if errors.Is(err, os.ErrExist) {
			return bad("root_incompatible")
		}
		return bad("indeterminate")
	}
	return nil
}

func ExecutionPath(id, entryTime string, config RootConfig) (string, error) {
	if !uuidPattern.MatchString(id) {
		return "", bad("schema_invalid")
	}
	entry, err := ParseTime(entryTime)
	if err != nil {
		return "", err
	}
	zone, err := partitionZone(config.PartitionTimezone)
	if err != nil {
		return "", err
	}
	date := entry.In(zone)
	if date.Year() < 1 || date.Year() > 9999 {
		return "", bad("invalid_time")
	}
	digest := sha256.Sum256([]byte(id))
	bucket := fmt.Sprintf("%x", digest)[0:1]
	return filepath.Join("executions", date.Format("20060102"), bucket, id+".jsonl"), nil
}
