package main

import (
	"bytes"
	"encoding/json"
	"strings"
	"testing"

	"github.com/cemc-oper/sledra/recorder/internal/buildinfo"
)

func TestInformationWithoutRecordingEnvironment(t *testing.T) {
	t.Setenv("SLEDRA_ROOT", "")
	t.Setenv("SLEDRA_CONFIG", "")
	t.Setenv("SLEDRA_SPEC_DIR", "/nonexistent/spec")
	for topic := range help {
		args := strings.Fields(topic)
		for _, form := range [][]string{append(append([]string{}, args...), "--help"), append([]string{"help"}, args...)} {
			var output bytes.Buffer
			if err := runWithOutput(form, &output); err != nil || output.String() != help[topic] {
				t.Fatalf("%v: %v; %s", form, err, output.String())
			}
		}
	}
	var output bytes.Buffer
	if err := runWithOutput([]string{"--version"}, &output); err != nil {
		t.Fatal(err)
	}
	var info buildinfo.Info
	if err := json.Unmarshal(output.Bytes(), &info); err != nil || info.ContractVersion != "1.0.0" || info.SoftwareVersion == "" || info.GoVersion == "" {
		t.Fatalf("version %s: %v", output.String(), err)
	}
}

func TestHelpDoesNotConsumeBusinessFlagValues(t *testing.T) {
	for _, args := range [][]string{
		{"context", "validate", "--file", "--help"},
		{"registry", "run-create", "--request-file", "-h"},
		{"event", "--type", "--help"},
	} {
		var output bytes.Buffer
		if handled, err := information(args, &output); handled || err != nil || output.Len() != 0 {
			t.Fatalf("business arguments consumed as help: %v; %v", args, err)
		}
	}
}
