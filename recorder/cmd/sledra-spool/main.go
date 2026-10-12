// sledra-spool appends fully encoded event input to a local qualified spool.
// The context-aware sledra event API belongs to SLD-08.
package main

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io"
	"os"

	"github.com/cemc-oper/sledra/recorder/event"
	"github.com/cemc-oper/sledra/recorder/spool"
)

type config struct {
	ProducerRole  string `json:"producer_role"`
	SegmentBytes  int64  `json:"segment_bytes"`
	SessionBytes  int64  `json:"session_bytes"`
	Segments      int    `json:"segments"`
	MaxSpoolBytes int64  `json:"max_spool_bytes"`
	MaxSpoolFiles int64  `json:"max_spool_files"`
}

func main() {
	if code := run(os.Args[1:], os.Stderr); code != 0 {
		os.Exit(code)
	}
}
func run(args []string, diag io.Writer) int {
	if len(args) == 0 || args[0] != "append" {
		report(diag, "usage", true)
		return 1
	}
	f := flag.NewFlagSet("append", flag.ContinueOnError)
	f.SetOutput(io.Discard)
	configPath := f.String("config", "", "")
	inputPath := f.String("input", "", "")
	strict := f.Bool("strict", false, "")
	seal := f.Bool("seal", false, "")
	if f.Parse(args[1:]) != nil || f.NArg() != 0 || *configPath == "" || *inputPath == "" {
		report(diag, "usage", true)
		return 1
	}
	failure := func(err error) int {
		report(diag, spool.Code(err), *strict)
		if *strict {
			return 1
		}
		return 0
	}
	root := os.Getenv("SLEDRA_ROOT")
	if root == "" {
		return failure(errors.New("root_missing"))
	}
	configFile, err := os.Open(*configPath)
	if err != nil {
		return failure(errors.New("config_invalid"))
	}
	defer configFile.Close()
	configBytes, err := io.ReadAll(io.LimitReader(configFile, 4097))
	if err != nil || len(configBytes) > 4096 {
		return failure(errors.New("config_invalid"))
	}
	var cfg config
	if json.Unmarshal(configBytes, &cfg) != nil || cfg.ProducerRole == "" {
		return failure(errors.New("config_invalid"))
	}
	inputFile, err := os.Open(*inputPath)
	if err != nil {
		return failure(errors.New("input_unavailable"))
	}
	defer inputFile.Close()
	input, err := event.ReadBounded(inputFile)
	if err != nil {
		return failure(err)
	}
	codec, err := event.New(cfg.ProducerRole)
	if err != nil {
		return failure(errors.New("contract_unavailable"))
	}
	encoded, err := codec.Validate(input)
	if err != nil {
		return failure(err)
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
	if json.Unmarshal(encoded.Bytes, &e) != nil {
		return failure(errors.New("schema_invalid"))
	}
	w, err := spool.Open(root, cfg.ProducerRole, e.Source.SourceID, e.Association.RunID, e.Association.TaskInstanceID, e.Association.AttemptID, e.Source.SessionID, spool.Limits{SegmentBytes: cfg.SegmentBytes, SessionBytes: cfg.SessionBytes, Segments: cfg.Segments, MaxSpoolBytes: cfg.MaxSpoolBytes, MaxSpoolFiles: cfg.MaxSpoolFiles})
	if err != nil {
		return failure(err)
	}
	err = w.Record(encoded.Bytes, *strict)
	if err == nil && *seal {
		err = w.Seal()
	}
	if closeErr := w.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return failure(err)
	}
	stats := w.Stats()
	if stats.Dropped > 0 {
		report(diag, stats.LastError, false)
	}
	return 0
}
func report(w io.Writer, code string, strict bool) {
	if code == "" {
		code = "spool_unavailable"
	}
	if len(code) > 64 {
		code = "spool_unavailable"
	}
	_, _ = fmt.Fprintf(w, "{\"ok\":false,\"code\":%q,\"strict\":%t}\n", code, strict)
}
