// sledra exposes the SLD-05 context lifecycle and SLD-07 event recording.
// These commands never execute or submit business work.
package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"flag"
	"io"
	"os"
	"path/filepath"

	"github.com/cemc-oper/sledra/recorder/context"
	"github.com/cemc-oper/sledra/recorder/internal/speccontract"
)

type runFile struct {
	context.RunRequest
	DefinitionFile  string `json:"definition_file"`
	ExpectationFile string `json:"expectation_file"`
}

const maxRequestBytes = 4 << 20

func main() {
	if err := run(os.Args[1:]); err != nil {
		_ = json.NewEncoder(os.Stderr).Encode(map[string]any{"ok": false, "code": err.Error()})
		os.Exit(1)
	}
}

func run(args []string) error {
	return runWithOutput(args, os.Stdout)
}

func runWithOutput(args []string, output io.Writer) error {
	if handled, err := information(args, output); handled {
		return err
	}
	if len(args) == 0 {
		return errors.New("usage")
	}
	if args[0] == "event" {
		return recordEvent(args[1:], os.Stderr)
	}
	if len(args) < 2 {
		return errors.New("usage")
	}
	root := os.Getenv("SLEDRA_ROOT")
	if root == "" {
		return errors.New("root_missing")
	}
	spec := os.Getenv("SLEDRA_SPEC_DIR")
	if spec != "" && !filepath.IsAbs(spec) {
		return errors.New("contract_path_invalid")
	}
	s, err := context.New(root, spec)
	if err != nil {
		return err
	}
	f := flag.NewFlagSet("sledra", flag.ContinueOnError)
	f.SetOutput(io.Discard)
	switch args[0] + " " + args[1] {
	case "registry run-create":
		request := f.String("request-file", "", "")
		out := f.String("context-out", "", "")
		if f.Parse(args[2:]) != nil || f.NArg() != 0 || *request == "" || *out == "" || !distinct(*request, *out) {
			return errors.New("usage")
		}
		var input runFile
		if err := readJSON(*request, &input); err != nil {
			return err
		}
		if input.DefinitionFile == "" {
			return errors.New("schema_invalid")
		}
		input.Definition, err = readMap(input.DefinitionFile)
		if err != nil {
			return err
		}
		if input.ExpectationFile != "" {
			input.Expectation, err = readMap(input.ExpectationFile)
			if err != nil {
				return err
			}
		}
		result, err := s.CreateRun(input.RunRequest)
		if err != nil {
			return err
		}
		return s.PublishContext(*out, result.Context)
	case "registry attempt-create":
		parent := f.String("context", "", "")
		request := f.String("request-file", "", "")
		out := f.String("context-out", "", "")
		if f.Parse(args[2:]) != nil || f.NArg() != 0 || *parent == "" || *request == "" || *out == "" || !distinct(*request, *out) || !distinct(*parent, *out) {
			return errors.New("usage")
		}
		run, err := s.LoadContext(*parent)
		if err != nil {
			return err
		}
		if run["kind"] != "run_context" {
			return errors.New("parent_mismatch")
		}
		var input context.AttemptRequest
		if err := readJSON(*request, &input); err != nil {
			return err
		}
		input.Run = run
		result, err := s.CreateAttempt(input)
		if err != nil {
			return err
		}
		return s.PublishContext(*out, result.Context)
	case "context validate":
		path := f.String("file", "", "")
		if f.Parse(args[2:]) != nil || f.NArg() != 0 || *path == "" {
			return errors.New("usage")
		}
		_, err := s.LoadContext(*path)
		return err
	case "run finish":
		parent := f.String("context", "", "")
		scope := f.String("scope-file", "", "")
		if f.Parse(args[2:]) != nil || f.NArg() != 0 || *parent == "" || *scope == "" {
			return errors.New("usage")
		}
		run, err := s.LoadContext(*parent)
		if err != nil {
			return err
		}
		if run["kind"] != "run_context" {
			return errors.New("parent_mismatch")
		}
		var input context.FinishRequest
		if err := readJSON(*scope, &input); err != nil {
			return err
		}
		input.Run = run
		_, err = s.Finish(input)
		return err
	default:
		return errors.New("usage")
	}
}

func distinct(a, b string) bool {
	x, e1 := filepath.Abs(a)
	y, e2 := filepath.Abs(b)
	return e1 == nil && e2 == nil && filepath.Clean(x) != filepath.Clean(y)
}

func readJSON(path string, out any) error {
	f, err := os.Open(path)
	if err != nil {
		return errors.New("input_unavailable")
	}
	defer f.Close()
	b, err := io.ReadAll(io.LimitReader(f, maxRequestBytes+1))
	if err != nil {
		return errors.New("input_unavailable")
	}
	if len(b) > maxRequestBytes {
		return errors.New("limit_exceeded")
	}
	if _, err := speccontract.Decode(b); err != nil {
		var ce *speccontract.ContractError
		if errors.As(err, &ce) {
			return errors.New(ce.Code)
		}
		return errors.New("schema_invalid")
	}
	d := json.NewDecoder(bytes.NewReader(b))
	d.UseNumber()
	d.DisallowUnknownFields()
	if err := d.Decode(out); err != nil {
		return errors.New("schema_invalid")
	}
	if err := d.Decode(new(any)); !errors.Is(err, io.EOF) {
		return errors.New("schema_invalid")
	}
	return nil
}

func readMap(path string) (map[string]any, error) {
	var doc map[string]any
	if err := readJSON(path, &doc); err != nil {
		return nil, err
	}
	if doc == nil {
		return nil, errors.New("schema_invalid")
	}
	return doc, nil
}
