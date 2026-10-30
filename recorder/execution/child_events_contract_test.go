package execution

import (
	"bytes"
	"encoding/json"
	"os"
	"slices"
	"testing"
)

// CE-01 is a synthetic contract stream, not output from a child-events runtime.
// Keep the production codec and its embedded 2.0.0 resource unchanged.
func TestChildEventsContractUsesUnchangedExecutionCodec(t *testing.T) {
	raw, err := os.ReadFile("../../spec/execution-child-events/cases/child-events.jsonl")
	if err != nil {
		t.Fatal(err)
	}
	codec, err := NewCodec()
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.HasSuffix(raw, []byte{'\n'}) {
		t.Fatal("missing final LF")
	}
	lines := bytes.Split(raw[:len(raw)-1], []byte{'\n'})
	if len(lines) != 8 {
		t.Fatalf("got %d records, want 8", len(lines))
	}
	var leads []string
	var executionID string
	for i, line := range lines {
		event, err := codec.Validate("event", line)
		if err != nil {
			t.Fatalf("record %d: %v", i+1, err)
		}
		if i == 0 {
			executionID = event["execution_id"].(string)
			if event["name"] != "execution.started" {
				t.Fatal("missing started")
			}
		}
		if event["execution_id"] != executionID {
			t.Fatal("foreign Execution")
		}
		attrs := event["attributes"].(map[string]any)
		if event["name"] == "gfs.modelvar.ready" {
			leads = append(leads, string(attrs["forecast_lead_hours"].(json.Number)))
		}
		if event["name"] == "execution.context" {
			params := attrs["parameters"].(map[string]any)
			if params["exact_steps"] != json.Number("9007199254740993") {
				t.Fatal("integer precision lost")
			}
		}
		if i == len(lines)-1 && event["name"] != "execution.ended" {
			t.Fatal("missing ended")
		}
	}
	if !slices.Equal(leads, []string{"0", "3", "6", "9", "12"}) {
		t.Fatalf("unexpected ready leads: %v", leads)
	}
}
