//go:build ignore

// Build the recorder tools with one offline recipe for development and packaging.
package main

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"io/fs"
	"os"
	"os/exec"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"time"
)

type Recipe struct {
	Target          []string          `json:"target"`
	CGOEnabled      string            `json:"cgo_enabled"`
	Flags           []string          `json:"flags"`
	Inputs          map[string]string `json:"inputs"`
	Provenance      map[string]string `json:"provenance"`
	SourceDateEpoch string            `json:"source_date_epoch"`
	Tools           []string          `json:"tools"`
}

type Manifest struct {
	Recipe    Recipe            `json:"recipe"`
	BuildTime string            `json:"build_time"`
	LDFlags   string            `json:"ldflags"`
	Binaries  map[string]string `json:"binaries"`
}

var validVersion = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9.+-]*$`)

func digest(path string) (string, error) {
	f, err := os.Open(path)
	if err != nil {
		return "", err
	}
	defer f.Close()
	h := sha256.New()
	if _, err := io.Copy(h, f); err != nil {
		return "", err
	}
	return hex.EncodeToString(h.Sum(nil)), nil
}

func command(dir string, env []string, name string, args ...string) ([]byte, error) {
	c := exec.Command(name, args...)
	c.Dir, c.Env = dir, env
	c.Stderr = os.Stderr
	return c.Output()
}

func run(dir string, env []string, name string, args ...string) error {
	c := exec.Command(name, args...)
	c.Dir, c.Env = dir, env
	c.Stdout, c.Stderr = os.Stdout, os.Stderr
	return c.Run()
}

func gitCommit(root string) string {
	top, err := exec.Command("git", "-C", root, "rev-parse", "--show-toplevel").Output()
	if err != nil || filepath.Clean(strings.TrimSpace(string(top))) != root {
		return "unknown"
	}
	head, err := exec.Command("git", "-C", root, "rev-parse", "HEAD").Output()
	if err != nil {
		return "unknown"
	}
	commit := strings.TrimSpace(string(head))
	status, err := exec.Command("git", "-C", root, "status", "--porcelain").Output()
	if err == nil && len(status) != 0 {
		commit += "-dirty"
	}
	return commit
}

func sourceInputs(root string) (map[string]string, error) {
	inputs := make(map[string]string)
	err := filepath.WalkDir(filepath.Join(root, "recorder"), func(path string, entry fs.DirEntry, err error) error {
		if err != nil || entry.IsDir() {
			return err
		}
		name := entry.Name()
		if filepath.Ext(name) != ".go" && filepath.Ext(name) != ".json" && name != "go.mod" && name != "go.sum" {
			return nil
		}
		rel, err := filepath.Rel(root, path)
		if err != nil {
			return err
		}
		inputs[filepath.ToSlash(rel)], err = digest(path)
		return err
	})
	if err != nil {
		return nil, err
	}
	for _, relative := range []string{"recorder/Makefile", "scripts/build_recorder.go"} {
		inputs[relative], err = digest(filepath.Join(root, relative))
		if err != nil {
			return nil, err
		}
	}
	return inputs, err
}

func buildTime() (string, error) {
	if raw := os.Getenv("SOURCE_DATE_EPOCH"); raw != "" {
		for _, char := range raw {
			if char < '0' || char > '9' {
				return "", errors.New("SOURCE_DATE_EPOCH must be a nonnegative integer timestamp")
			}
		}
		epoch, err := strconv.ParseInt(raw, 10, 64)
		if err != nil {
			return "", err
		}
		return time.Unix(epoch, 0).UTC().Format("2006-01-02T15:04:05Z"), nil
	}
	return time.Now().UTC().Format("2006-01-02T15:04:05Z"), nil
}

func build(output string, tools []string) error {
	root, err := filepath.Abs("..")
	if err != nil {
		return err
	}
	recorder := filepath.Join(root, "recorder")
	env := append(
		os.Environ(),
		"GOPROXY=off", "GOSUMDB=off", "GONOPROXY=none", "GOTOOLCHAIN=local", "CGO_ENABLED=0",
		"GOOS="+os.Getenv("SLEDRA_TARGET_GOOS"), "GOARCH="+os.Getenv("SLEDRA_TARGET_GOARCH"),
	)
	goTarget, err := command(recorder, env, "go", "env", "GOVERSION", "GOOS", "GOARCH")
	if err != nil {
		return err
	}
	target := strings.Fields(string(goTarget))
	if len(target) != 3 {
		return errors.New("go env returned an incomplete target")
	}
	inputs, err := sourceInputs(root)
	if err != nil {
		return err
	}
	encoded, err := json.Marshal(inputs)
	if err != nil {
		return err
	}
	source := sha256.Sum256(encoded)
	version := os.Getenv("SLEDRA_VERSION")
	if version == "" {
		version = "dev"
	}
	if !validVersion.MatchString(version) {
		return errors.New("SLEDRA_VERSION must contain only letters, digits, dots, plus or hyphens")
	}
	provenance := map[string]string{
		"software_version": version,
		"commit":           gitCommit(root),
		"source_sha256":    hex.EncodeToString(source[:]),
	}
	recipe := Recipe{target, "0", []string{"-trimpath", "-buildvcs=false"}, inputs, provenance, os.Getenv("SOURCE_DATE_EPOCH"), tools}
	stamp, err := buildTime()
	if err != nil {
		return err
	}
	output, err = filepath.Abs(output)
	if err != nil {
		return err
	}
	manifestPath := filepath.Join(output, "manifest.json")
	var previous Manifest
	if data, err := os.ReadFile(manifestPath); err == nil {
		_ = json.Unmarshal(data, &previous)
	}
	before, _ := json.Marshal(previous.Recipe)
	after, _ := json.Marshal(recipe)
	sameRecipe := string(before) == string(after)
	if sameRecipe && previous.BuildTime != "" {
		stamp = previous.BuildTime
	}
	ldflags := fmt.Sprintf("-X github.com/cemc-oper/sledra/recorder/internal/buildinfo.Version=%s -X github.com/cemc-oper/sledra/recorder/internal/buildinfo.Commit=%s -X github.com/cemc-oper/sledra/recorder/internal/buildinfo.BuildTime=%s -X github.com/cemc-oper/sledra/recorder/internal/buildinfo.SourceSHA256=%s", version, provenance["commit"], stamp, provenance["source_sha256"])
	binDir := filepath.Join(output, "bin")
	if err := os.MkdirAll(binDir, 0o755); err != nil {
		return err
	}
	binaries := make(map[string]string)
	for _, name := range tools {
		binary := filepath.Join(binDir, name)
		current, hashErr := digest(binary)
		info, statErr := os.Stat(binary)
		if sameRecipe && hashErr == nil && statErr == nil && info.Mode()&0o111 != 0 && previous.Binaries[name] == current {
			fmt.Println("Reuse", binary)
		} else {
			fmt.Println("Build", binary)
			temporary, err := os.CreateTemp(binDir, "."+name+"-*.tmp")
			if err != nil {
				return err
			}
			path := temporary.Name()
			_ = temporary.Close()
			defer os.Remove(path)
			if err := run(recorder, env, "go", "build", "-trimpath", "-buildvcs=false", "-ldflags", ldflags, "-o", path, "./cmd/"+name); err != nil {
				return err
			}
			if err := os.Rename(path, binary); err != nil {
				return err
			}
		}
		binaries[name], err = digest(binary)
		if err != nil {
			return err
		}
	}
	manifest := Manifest{recipe, stamp, ldflags, binaries}
	data, err := json.MarshalIndent(manifest, "", "  ")
	if err != nil {
		return err
	}
	tmp, err := os.CreateTemp(output, ".manifest-*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(tmp.Name())
	if _, err := tmp.Write(append(data, '\n')); err != nil {
		return err
	}
	if err := tmp.Close(); err != nil {
		return err
	}
	return os.Rename(tmp.Name(), manifestPath)
}

func main() {
	output := flag.String("output", "../build", "shared build output directory")
	toolNames := flag.String("tools", "sledra,sledra-spec-check,sledra-event,sledra-spool,sledra-retention", "comma-separated recorder tools")
	flag.Parse()
	tools := strings.Split(*toolNames, ",")
	for _, tool := range tools {
		if !regexp.MustCompile(`^sledra(?:-[a-z]+)*$`).MatchString(tool) {
			fmt.Fprintln(os.Stderr, "invalid tool name:", tool)
			os.Exit(1)
		}
	}
	if err := build(*output, tools); err != nil {
		fmt.Fprintln(os.Stderr, "recorder build failed:", err)
		os.Exit(1)
	}
}
