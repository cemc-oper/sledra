// sledra-event is an offline encoding/validation entry point. It does not
// append to a spool; SLD-07 owns that operation.
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"os"
	"path/filepath"
	"strings"

	"github.com/cemc-oper/sledra/recorder/event"
)

type config struct {
	ProducerRole string `json:"producer_role"`
}

func main() {
	if err := run(os.Args[1:]); err != nil {
		report(err)
		os.Exit(1)
	}
}

func run(args []string) error {
	if len(args) == 0 || (args[0] != "encode" && args[0] != "validate") {
		return errors.New("usage")
	}
	mode := args[0]
	f := flag.NewFlagSet(mode, flag.ContinueOnError)
	f.SetOutput(io.Discard)
	configPath := f.String("config", "", "installed JSON config with producer_role")
	input := f.String("input", "", "event JSON file")
	outputDir := f.String("output-dir", "", "existing output directory for encode")
	if err := f.Parse(args[1:]); err != nil || f.NArg() != 0 || *configPath == "" || *input == "" || (mode == "encode" && *outputDir == "") {
		return errors.New("usage")
	}
	cfgFile, err := os.Open(*configPath)
	if err != nil {
		return errors.New("config_unavailable")
	}
	defer cfgFile.Close()
	cfgBytes, err := io.ReadAll(io.LimitReader(cfgFile, 4097))
	if err != nil || len(cfgBytes) > 4096 {
		return errors.New("config_invalid")
	}
	var cfg config
	d := json.NewDecoder(bytes.NewReader(cfgBytes))
	d.DisallowUnknownFields()
	if err := d.Decode(&cfg); err != nil || cfg.ProducerRole == "" {
		return errors.New("config_invalid")
	}
	if err := d.Decode(new(any)); !errors.Is(err, io.EOF) {
		return errors.New("config_invalid")
	}
	codec, err := event.New(cfg.ProducerRole)
	if err != nil {
		return errors.New("contract_unavailable")
	}
	file, err := os.Open(*input)
	if err != nil {
		return errors.New("input_unavailable")
	}
	defer file.Close()
	data, err := event.ReadBounded(file)
	if err != nil {
		var e *event.Error
		if !errors.As(err, &e) {
			return errors.New("input_unavailable")
		}
		return err
	}
	result, err := codec.Validate(data)
	if err != nil {
		return err
	}
	if mode == "validate" {
		return nil
	}
	var id struct {
		EventID string `json:"event_id"`
	}
	if err := json.Unmarshal(result.Bytes, &id); err != nil {
		return errors.New("schema_invalid")
	}
	if strings.ContainsAny(id.EventID, "/\\") {
		return errors.New("schema_invalid")
	}
	return publish(*outputDir, id.EventID+".json", result.Bytes)
}

func publish(dir, name string, data []byte) error {
	if !realDirectory(dir) {
		return errors.New("output_unavailable")
	}
	path := filepath.Join(dir, name)
	if existing, err := os.ReadFile(path); err == nil {
		if bytes.Equal(existing, data) {
			return nil
		}
		return errors.New("conflicting_event_id")
	} else if !errors.Is(err, os.ErrNotExist) {
		return errors.New("output_unavailable")
	}
	tmp, err := os.CreateTemp(dir, ".sledra-event-*")
	if err != nil {
		return errors.New("output_unavailable")
	}
	defer os.Remove(tmp.Name())
	if err := tmp.Chmod(0600); err != nil {
		tmp.Close()
		return errors.New("output_unavailable")
	}
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		return errors.New("output_unavailable")
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		return errors.New("output_unavailable")
	}
	if err := tmp.Close(); err != nil {
		return errors.New("output_unavailable")
	}
	if err := os.Link(tmp.Name(), path); err != nil {
		if errors.Is(err, os.ErrExist) {
			winner, readErr := os.ReadFile(path)
			if readErr == nil && bytes.Equal(winner, data) {
				return nil
			}
			return errors.New("conflicting_event_id")
		}
		return errors.New("output_unavailable")
	}
	d, err := os.Open(dir)
	if err != nil {
		return errors.New("indeterminate")
	}
	defer d.Close()
	if err := d.Sync(); err != nil {
		return errors.New("indeterminate")
	}
	return nil
}

func realDirectory(dir string) bool {
	abs, err := filepath.Abs(dir)
	if err != nil {
		return false
	}
	part := string(os.PathSeparator)
	for _, component := range strings.Split(strings.TrimPrefix(abs, part), part) {
		if component == "" {
			continue
		}
		part = filepath.Join(part, component)
		info, err := os.Lstat(part)
		if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return false
		}
	}
	return true
}

func report(err error) {
	code := err.Error()
	var e *event.Error
	if errors.As(err, &e) {
		code = e.Code
	}
	if code == "" {
		code = "internal_error"
	}
	_ = json.NewEncoder(os.Stderr).Encode(map[string]any{"ok": false, "code": code})
}
