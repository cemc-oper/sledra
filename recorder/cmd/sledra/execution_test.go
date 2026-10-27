package main

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/cemc-oper/sledra/recorder/execution"
)

func executionTestCall(t *testing.T, args []string, input string, status int, code string) string {
	t.Helper()
	var output bytes.Buffer
	err := executionCommand(args, strings.NewReader(input), &output)
	if status == 0 {
		if err != nil {
			t.Fatal(args, err)
		}
	} else {
		var failure *executionError
		if !errors.As(err, &failure) || failure.status != status || (code != "" && failure.code != code) || output.Len() != 0 {
			t.Fatalf("%v: error=%v output=%q; want %d/%s", args, err, output.String(), status, code)
		}
	}
	return output.String()
}

func executionTestReceipt(t *testing.T, args ...string) (string, string, string, map[string]any) {
	t.Helper()
	out := executionTestCall(t, append([]string{"prepare"}, args...), "", 0, "")
	fields := strings.Split(strings.TrimSuffix(out, "\n"), "\t")
	if len(out) > 65792 || strings.Count(out, "\n") != 1 || len(fields) != 3 {
		t.Fatal("invalid receipt", out)
	}
	codec, err := execution.NewCodec()
	if err != nil {
		t.Fatal(err)
	}
	event, err := codec.Validate("event", []byte(fields[2]))
	if err != nil || event["execution_id"] != fields[0] {
		t.Fatal("invalid event", err)
	}
	return fields[0], fields[1], fields[2], event
}

func executionTestInit(t *testing.T, root string) {
	t.Helper()
	executionTestCall(t, []string{"init-root", "--root", root, "--system", "synthetic", "--deployment", "local", "--partition-timezone=+08:00"}, "", 0, "")
}

func TestExecutionPreparationIsIndependent(t *testing.T) {
	root := filepath.Join(t.TempDir(), "must-not-exist")
	t.Setenv("SLEDRA_ROOT", root)
	t.Setenv("SLEDRA_CONFIG", "/missing-producer")
	t.Setenv("SLEDRA_SPEC_DIR", "relative-invalid-legacy")
	var output bytes.Buffer
	if err := runWithOutput([]string{"execution", "prepare", "start"}, &output); err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(root); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("prepare touched root", err)
	}
	id, entry, _, event := executionTestReceipt(t, "start")
	if entry != event["occurred_at"] || !strings.HasSuffix(entry, "Z") || event["attributes"].(map[string]any)["task"] != nil {
		t.Fatal(event)
	}
	id2, _, _, _ := executionTestReceipt(t, "start", "--task", "预报", "--engine-kind", "synthetic", "--engine-alias", "false")
	if id == id2 {
		t.Fatal("identity reused")
	}
	_, _, _, known := executionTestReceipt(t, "start", "--engine-kind", "test")
	engine := known["attributes"].(map[string]any)["engine"].(map[string]any)
	if engine["service"] != nil || engine["node"] != nil {
		t.Fatal(engine)
	}
	if _, exists := engine["alias"]; exists {
		t.Fatal("default alias invented")
	}
}

func TestExecutionStrictPreparation(t *testing.T) {
	id, _, _, _ := executionTestReceipt(t, "start")
	context := []string{"prepare", "context", "--execution-id", id, "--task", "task", "--cycle-kind", "none"}
	bad := [][]string{
		{}, {"bogus"}, {"prepare"}, {"prepare", "event"}, {"prepare", "stage-start"},
		{"prepare", "start", "--task"}, {"prepare", "start", "--task", ""},
		{"prepare", "start", "--task", "a", "--task=b"}, {"prepare", "start", "extra"},
		{"prepare", "start", "--engine-node", "node"}, {"prepare", "start", "--engine-kind", "test", "--engine-alias", "TRUE"},
		{"prepare", "start", "--execution-id", id}, {"append", "--create=true"},
		{"prepare", "context", "--execution-id", id, "--task", "task"},
		{"prepare", "context", "--execution-id", id, "--task", "task", "--cycle-kind", "single"},
		{"prepare", "end", "--execution-id", id},
		{"prepare", "end", "--execution-id", id, "--exit-code", "0", "--signal", "15"},
		{"prepare", "end", "--execution-id", id, "--exit-code", "1.0"},
		{"prepare", "end", "--execution-id", id, "--exit-code", "true"},
		{"prepare", "end", "--execution-id", id, "--exit-code", "256"},
		{"prepare", "end", "--execution-id", id, "--signal", "0"},
		{"prepare", "end", "--execution-id", id, "--signal", "128"},
	}
	for _, suffix := range [][]string{
		{"--cycle --scope", "must-not-be-ignored"},
		{"--cycle", "2026-10-27T00:00:00Z"}, {"--run-id", id}, {"--unknown", "secret"},
		{"--task=other"}, {"--string", "key"}, {"--string", "key", "a", "--null", "key"},
		{"--string", "Bad-Key", "a"}, {"--bool", "key", "True"}, {"--bool", "key", "0"},
		{"--int", "key", "01"}, {"--int", "key", "1e0"}, {"--int", "key", " 1"}, {"--int", "key", "\"1\""},
		{"--int", "key", "9223372036854775808"}, {"--float", "key", "1"},
		{"--float", "key", "NaN"}, {"--float", "key", "1e999"}, {"--float", "key", "1e-999"},
		{"--string", "key", strings.Repeat("x", 4097)}, {"--previous-event", "{}"},
	} {
		bad = append(bad, append(append([]string{}, context...), suffix...))
	}
	for _, args := range bad {
		t.Run(strings.Join(args[:min(4, len(args))], " "), func(t *testing.T) { executionTestCall(t, args, "", 2, "") })
	}
	for _, kind := range []string{"none", "multiple"} {
		executionTestReceipt(t, "context", "--execution-id", id, "--task", "task", "--cycle-kind", kind)
	}
	for _, result := range [][]string{{"--exit-code", "0"}, {"--exit-code", "255"}, {"--signal", "1"}, {"--signal", "127"}} {
		executionTestReceipt(t, append([]string{"end", "--execution-id", id}, result...)...)
	}
}

func TestExecutionContextAndEndIdempotency(t *testing.T) {
	id, _, _, _ := executionTestReceipt(t, "start")
	text := "预报\n empty $HOME $(touch must-not-run) `literal`"
	args := []string{"context", "--execution-id", id, "--task", "task", "--cycle-kind", "single", "--cycle", "2026-10-27T08:00:00.000000001+08:00", "--scope", "forecast", "--int", "large", "9007199254740993", "--float", "decimal", "0.10000000000000001", "--bool", "enabled", "false", "--null", "missing", "--string", "message", text, "--string", "empty", ""}
	_, aux, original, event := executionTestReceipt(t, args...)
	attrs := event["attributes"].(map[string]any)
	params := attrs["parameters"].(map[string]any)
	if aux != "-" || attrs["run_id"] != nil || params["message"] != text || params["empty"] != "" || params["large"] != json.Number("9007199254740993") {
		t.Fatal(event)
	}
	args[8] = "2026-10-27T00:00:00.000000001Z"
	_, _, repeated, _ := executionTestReceipt(t, append(args, "--previous-event", original)...)
	if repeated != original {
		t.Fatal("retransmit changed identity/time/bytes")
	}
	args[8] = "2026-10-27T00:00:00.000000002Z"
	executionTestCall(t, append(append([]string{"prepare"}, args...), "--previous-event", original), "", 3, "conflicting_context")
	other, _, _, _ := executionTestReceipt(t, "start")
	executionTestCall(t, []string{"prepare", "context", "--execution-id", other, "--task", "task", "--cycle-kind", "none", "--previous-event", original}, "", 2, "execution_mismatch")
	_, _, end, _ := executionTestReceipt(t, "end", "--execution-id", id, "--exit-code", "7")
	_, _, end2, _ := executionTestReceipt(t, "end", "--execution-id", id, "--exit-code", "7", "--previous-event", end)
	if end != end2 {
		t.Fatal("end changed")
	}
	executionTestCall(t, []string{"prepare", "end", "--execution-id", id, "--signal", "7", "--previous-event", end}, "", 3, "conflicting_end")
}

func TestExecutionAppendFramingAndIsolation(t *testing.T) {
	root := filepath.Join(t.TempDir(), "records")
	executionTestInit(t, root)
	executionTestInit(t, root)
	id, entry, start, _ := executionTestReceipt(t, "start")
	args := []string{"append", "--root", root, "--execution-id", id, "--entry-time", entry}
	create := append(append([]string{}, args...), "--create")
	for _, line := range []string{"", start, start + "\r\n", start + "\n\n", start + "\n{}\n", strings.Repeat("x", 65537) + "\n"} {
		executionTestCall(t, create, line, 2, "")
	}
	files, _ := filepath.Glob(filepath.Join(root, "executions", "*", "*", "*"))
	if len(files) != 0 {
		t.Fatal("validation created files", files)
	}
	wrongTime := append([]string{}, create...)
	wrongTime[6] = "2026-01-01T00:00:00Z"
	executionTestCall(t, wrongTime, start+"\n", 2, "entry_mismatch")
	executionTestCall(t, create, start+"\n", 0, "")
	files, _ = filepath.Glob(filepath.Join(root, "executions", "*", "*", "*"))
	before, _ := os.ReadFile(files[0])
	executionTestCall(t, create, start+"\n", 3, "")
	executionTestCall(t, args, start+"\n", 2, "schema_invalid")
	_, _, end, _ := executionTestReceipt(t, "end", "--execution-id", id, "--exit-code", "0")
	// Exactly 65536 object bytes plus LF must fit; one byte more must fail.
	boundary := end + strings.Repeat(" ", execution.MaxRecordBytes-len(end))
	executionTestCall(t, args, boundary+" \n", 2, "limit_exceeded")
	executionTestCall(t, args, boundary+"\n", 0, "")
	after, _ := os.ReadFile(files[0])
	if string(after) != string(before)+boundary+"\n" {
		t.Fatal("existing bytes changed")
	}
	if err := os.WriteFile(files[0], append(after, []byte("partial")...), 0600); err != nil {
		t.Fatal(err)
	}
	executionTestCall(t, args, end+"\n", 3, "incomplete_tail")
	if err := os.Remove(files[0]); err != nil {
		t.Fatal(err)
	}
	executionTestCall(t, args, end+"\n", 3, "")
	if _, err := os.Stat(files[0]); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("missing file rebuilt")
	}
}

func TestExecutionRootAndLegacyDispatch(t *testing.T) {
	root := filepath.Join(t.TempDir(), "records")
	executionTestInit(t, root)
	config, _ := os.ReadFile(filepath.Join(root, "sledra.json"))
	executionTestCall(t, []string{"init-root", "--root", root, "--system", "different", "--deployment", "local", "--partition-timezone", "+08:00"}, "", 3, "root_incompatible")
	after, _ := os.ReadFile(filepath.Join(root, "sledra.json"))
	if !bytes.Equal(config, after) {
		t.Fatal("root overwritten")
	}
	t.Setenv("SLEDRA_ROOT", root)
	t.Setenv("SLEDRA_SPEC_DIR", "/missing-legacy")
	t.Setenv("SLEDRA_CONFIG", "/must-not-load-producer")
	for _, args := range [][]string{{"registry", "run-create"}, {"registry", "attempt-create"}, {"run", "finish"}, {"context", "validate"}} {
		if err := run(args); err == nil || err.Error() != "root_incompatible" {
			t.Fatal(args, err)
		}
	}
	var diag bytes.Buffer
	for _, strict := range []bool{false, true} {
		args := []string{"--context", "missing", "--type", "attempt.started"}
		if strict {
			args = append(args, "--strict")
		}
		err := recordEvent(args, &diag)
		if strict != (err != nil) {
			t.Fatal("legacy event policy changed", err)
		}
		if strict && err.Error() != "spool_unavailable" {
			t.Fatal("root refusal did not precede legacy input loading", err)
		}
	}
	if !strings.Contains(diag.String(), `"dropped":1`) || !strings.Contains(diag.String(), `"code":"spool_unavailable"`) {
		t.Fatal(diag.String())
	}
	entries, _ := os.ReadDir(root)
	if len(entries) != 1 || entries[0].Name() != "sledra.json" {
		t.Fatal("legacy side effects", entries)
	}
	unknown := t.TempDir()
	if err := os.WriteFile(filepath.Join(unknown, "keep"), []byte("unchanged"), 0600); err != nil {
		t.Fatal(err)
	}
	executionTestCall(t, []string{"init-root", "--root", unknown, "--system", "synthetic", "--deployment", "local", "--partition-timezone", "+00:00"}, "", 3, "root_incompatible")
	invalid := filepath.Join(t.TempDir(), "invalid")
	executionTestCall(t, []string{"init-root", "--root", invalid, "--system", "synthetic", "--deployment", "local", "--partition-timezone", "+15:00"}, "", 2, "")
	if _, err := os.Stat(invalid); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("invalid config touched filesystem")
	}
}

type failedReceipt struct{}

func (failedReceipt) Write(p []byte) (int, error) { return len(p) / 2, io.ErrClosedPipe }

func TestExecutionReceiptWriteFailure(t *testing.T) {
	err := executionCommand([]string{"prepare", "start"}, strings.NewReader(""), failedReceipt{})
	var failure *executionError
	if !errors.As(err, &failure) || failure.status != 3 || failure.code != "indeterminate" {
		t.Fatal(err)
	}
}

func TestExecutionVersionSeparatesLegacyContract(t *testing.T) {
	var output bytes.Buffer
	if err := runWithOutput([]string{"--version"}, &output); err != nil {
		t.Fatal(err)
	}
	var info map[string]any
	if err := json.Unmarshal(output.Bytes(), &info); err != nil {
		t.Fatal(err)
	}
	if info["contract_version"] != "1.0.0" || info["execution_format_version"] != "2.0.0" || info["execution_layout_version"] != float64(2) {
		t.Fatal(info)
	}
}
