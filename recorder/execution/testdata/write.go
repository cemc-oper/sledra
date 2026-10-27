// Test-only producer for the unchanged Python P0 validator; not a user CLI.
package main

import (
	"encoding/json"
	"fmt"
	"os"

	"github.com/cemc-oper/sledra/recorder/execution"
)

func main() {
	if err := write(); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func write() error {
	if len(os.Args) != 2 {
		return fmt.Errorf("test root required")
	}
	settings := execution.RootConfig{
		FormatVersion: execution.FormatVersion, LayoutVersion: execution.LayoutVersion,
		Namespace: execution.Namespace{System: "example", Deployment: "test"}, PartitionTimezone: "+08:00",
	}
	if err := execution.InitRoot(os.Args[1], settings); err != nil {
		return err
	}
	r, err := execution.OpenRoot(os.Args[1])
	if err != nil {
		return err
	}
	defer r.Close()
	c, err := execution.NewCodec()
	if err != nil {
		return err
	}
	start, err := c.NewStart(nil, nil)
	if err != nil {
		return err
	}
	event, err := c.Validate("event", start)
	if err != nil {
		return err
	}
	w, err := r.Create(start)
	if err != nil {
		return err
	}
	defer w.Close()
	for _, fact := range []struct {
		name  string
		attrs map[string]any
	}{
		{"execution.context", map[string]any{"task": "example.check", "parameters": map[string]any{"member": "01"}, "scope": nil, "cycle_kind": "none", "cycle": nil, "run_id": nil}},
		{"example.progress", map[string]any{"steps": json.Number("9007199254740993"), "fraction": json.Number("0.125"), "message": "预报\nready"}},
		{"execution.ended", map[string]any{"exit_code": 0}},
	} {
		raw, err := c.NewEvent(event["execution_id"].(string), fact.name, fact.attrs)
		if err != nil {
			return err
		}
		if err := w.Append(raw); err != nil {
			return err
		}
	}
	if err := w.Close(); err != nil {
		return err
	}
	fmt.Println(w.Path())
	return nil
}
