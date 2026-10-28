package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestExecutionBusinessEventAttributes(t *testing.T) {
	missing := filepath.Join(t.TempDir(), "must-not-create")
	t.Setenv("SLEDRA_ROOT", missing)
	id, _, _, _ := executionTestReceipt(t, "start")
	args := []string{"event", "forecast.progress", "--execution-id", id,
		"--int", "steps", "9007199254740993", "--int", "negative", "-9223372036854775808",
		"--float", "decimal", "0.10000000000000001", "--bool", "ready", "false",
		"--null", "absent", "--string", "empty", "", "--string", "任意 key", "中文\n$HOME $(touch not-executed) `literal` \" \\"}
	_, aux, raw, event := executionTestReceipt(t, args...)
	attrs := event["attributes"].(map[string]any)
	if aux != "-" || event["name"] != "forecast.progress" || len(attrs) != 7 ||
		attrs["steps"] != json.Number("9007199254740993") || attrs["negative"] != json.Number("-9223372036854775808") ||
		attrs["decimal"] != json.Number("0.10000000000000001") || attrs["ready"] != false || attrs["absent"] != nil || attrs["empty"] != "" {
		t.Fatal(event)
	}
	_, _, second, _ := executionTestReceipt(t, args...)
	if raw == second {
		t.Fatal("new identical business call reused event identity")
	}
	if _, err := os.Stat(missing); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("prepare touched root", err)
	}
}

func TestExecutionBusinessEventRejections(t *testing.T) {
	id, _, _, _ := executionTestReceipt(t, "start")
	for _, name := range []string{"execution.started", "execution.context", "execution.ended", "execution.future", "stage.started", "stage.ended", "stage.future"} {
		t.Run(name, func(t *testing.T) {
			executionTestCall(t, []string{"prepare", "event", name, "--execution-id", id}, "", 2, "reserved_event_name")
		})
	}
	for _, name := range []string{"", "no_dot", "Forecast.progress", "forecast.progress\n", strings.Repeat("x", 128) + ".event"} {
		executionTestCall(t, []string{"prepare", "event", name, "--execution-id", id}, "", 2, "")
	}
	base := []string{"prepare", "event", "forecast.progress", "--execution-id", id}
	for _, suffix := range [][]string{
		{"--previous-event", "{}"}, {"--event-id", id}, {"--execution-id", id}, {"--task", "task"}, {"extra"},
		{"--int", "key", "1e0"}, {"--float", "key", "1"}, {"--float", "key", "NaN"}, {"--float", "key", "1e-999"},
		{"--bool", "key", "TRUE"}, {"--int", "key", "9223372036854775808"},
		{"--string", "key", "first", "--null", "key"}, {"--null", "key", "extra"},
		{"--string", "key"}, {"--string", strings.Repeat("x", 129), "value"}, {"--string", "key", strings.Repeat("x", 4097)},
	} {
		executionTestCall(t, append(append([]string{}, base...), suffix...), "", 2, "")
	}
	// NAME immediately follows event; typed values after it retain fixed arities.
	executionTestCall(t, []string{"prepare", "event", "--execution-id", id, "forecast.progress"}, "", 2, "")
}

func TestExecutionBusinessEventLimits(t *testing.T) {
	id, _, _, _ := executionTestReceipt(t, "start")
	base := []string{"event", "forecast.progress", "--execution-id", id}
	executionTestReceipt(t, append(append([]string{}, base...), "--string", strings.Repeat("k", 128), strings.Repeat("🙂", 1024))...)
	for _, n := range []int{128, 129} {
		args := append([]string{}, base...)
		for i := 0; i < n; i++ {
			args = append(args, "--null", fmt.Sprintf("key_%d", i))
		}
		if n == 128 {
			executionTestReceipt(t, args...)
		} else {
			executionTestCall(t, append([]string{"prepare"}, args...), "", 2, "limit_exceeded")
		}
	}
	args := append([]string{}, base...)
	for i := 0; i < 15; i++ {
		args = append(args, "--string", fmt.Sprintf("key_%d", i), strings.Repeat("x", 4096))
	}
	executionTestReceipt(t, args...)
	args = append(args, "--string", "last", strings.Repeat("x", 4096))
	executionTestCall(t, append([]string{"prepare"}, args...), "", 2, "limit_exceeded")
}

func TestExecutionSequentialStagePreparation(t *testing.T) {
	id, _, _, _ := executionTestReceipt(t, "start")
	stage := "积分\n重复阶段"
	_, firstID, firstRaw, first := executionTestReceipt(t, "stage-start", "--execution-id", id, "--stage", stage)
	_, secondID, secondRaw, _ := executionTestReceipt(t, "stage-start", "--execution-id", id, "--stage", stage)
	if firstID == secondID || firstRaw == secondRaw || first["name"] != "stage.started" ||
		first["attributes"].(map[string]any)["stage_id"] != firstID {
		t.Fatal("stage intervals reused identity", first)
	}
	for _, result := range [][]string{{"--exit-code", "7"}, {"--signal", "127"}} {
		_, aux, _, end := executionTestReceipt(t, append([]string{"stage-end", "--execution-id", id, "--stage", stage, "--stage-id", firstID}, result...)...)
		attrs := end["attributes"].(map[string]any)
		if aux != "-" || end["name"] != "stage.ended" || attrs["stage_id"] != firstID || attrs["stage"] != stage || len(attrs) != 3 {
			t.Fatal(end)
		}
		if _, exists := attrs["duration_ns"]; exists {
			t.Fatal("duration fabricated")
		}
	}
	for _, suffix := range [][]string{
		{}, {"--exit-code", "256"}, {"--exit-code", "1.0"}, {"--signal", "0"}, {"--signal", "128"},
		{"--exit-code", "0", "--signal", "15"}, {"--exit-code", "0", "--previous-event", firstRaw},
		{"--exit-code", "0", "--duration-ns", "0"}, {"--exit-code", "0", "--string", "key", "value"},
	} {
		executionTestCall(t, append([]string{"prepare", "stage-end", "--execution-id", id, "--stage", stage, "--stage-id", firstID}, suffix...), "", 2, "")
	}
	for _, args := range [][]string{
		{"stage-start", "--execution-id", id}, {"stage-start", "--execution-id", id, "--stage", ""},
		{"stage-start", "--execution-id", id, "--stage", strings.Repeat("阶", 129)},
		{"stage-start", "--execution-id", id, "--stage", stage, "--stage-id", firstID},
		{"stage-end", "--execution-id", id, "--stage", stage, "--exit-code", "0"},
		{"stage-end", "--execution-id", id, "--stage", stage, "--stage-id", "bad-id", "--exit-code", "0"},
	} {
		executionTestCall(t, append([]string{"prepare"}, args...), "", 2, "")
	}
	executionTestReceipt(t, "stage-start", "--execution-id", id, "--stage", strings.Repeat("阶", 128))
}

func TestExecutionCachedBusinessEventRetransmission(t *testing.T) {
	root := filepath.Join(t.TempDir(), "records")
	executionTestInit(t, root)
	id, entry, start, _ := executionTestReceipt(t, "start")
	args := []string{"append", "--root", root, "--execution-id", id, "--entry-time", entry}
	executionTestCall(t, append(append([]string{}, args...), "--create"), start+"\n", 0, "")
	_, _, first, _ := executionTestReceipt(t, "event", "forecast.progress", "--execution-id", id, "--int", "step", "1")
	_, _, second, _ := executionTestReceipt(t, "event", "forecast.progress", "--execution-id", id, "--int", "step", "1")
	// Test-only retransmission after known successful writes: replay the cached
	// complete event, without preparation or allocating another event identity.
	for _, raw := range []string{first, first, second} {
		executionTestCall(t, args, raw+"\n", 0, "")
	}
	files, err := filepath.Glob(filepath.Join(root, "executions", "*", "*", "*.jsonl"))
	if err != nil || len(files) != 1 {
		t.Fatal(files, err)
	}
	raw, err := os.ReadFile(files[0])
	if err != nil || string(raw) != start+"\n"+first+"\n"+first+"\n"+second+"\n" || first == second {
		t.Fatal("retransmission changed the fact", err)
	}
}
