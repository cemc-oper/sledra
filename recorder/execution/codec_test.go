package execution

import (
	"bytes"
	"encoding/json"
	"math"
	"os"
	"path/filepath"
	"runtime"
	"sort"
	"strings"
	"testing"
)

func specDir() string {
	_, source, _, _ := runtime.Caller(0)
	return filepath.Join(filepath.Dir(source), "../../spec/execution-v2")
}

func fixture(t *testing.T, name string) []byte {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(specDir(), "cases", name))
	if err != nil {
		t.Fatal(err)
	}
	return raw
}

func codec(t *testing.T) *Codec {
	t.Helper()
	c, err := NewCodec()
	if err != nil {
		t.Fatal(err)
	}
	return c
}

func wantCode(t *testing.T, err error, code string) {
	t.Helper()
	if err == nil || Code(err) != code {
		t.Fatalf("want %s, got %v", code, err)
	}
}

func TestSharedContractCases(t *testing.T) {
	c := codec(t)
	var cases []struct {
		File, Kind, Expected string
		Conflicts            []string
	}
	if err := json.Unmarshal(fixture(t, "cases.json"), &cases); err != nil {
		t.Fatal(err)
	}
	for _, tc := range cases {
		t.Run(tc.File, func(t *testing.T) {
			raw := fixture(t, tc.File)
			if tc.Kind == "stream" || tc.Kind == "run-set" {
				var records [][]byte
				kind := "event"
				if tc.Kind == "stream" {
					records = bytes.Split(bytes.TrimSuffix(raw, []byte{'\n'}), []byte{'\n'})
				} else {
					kind = "run"
					var items []json.RawMessage
					if err := json.Unmarshal(raw, &items); err != nil {
						t.Fatal(err)
					}
					for _, item := range items {
						records = append(records, item)
					}
				}
				seen, contexts := map[string]map[string]any{}, map[string]any{}
				conflicts := map[string]bool{}
				for _, input := range records {
					m, err := c.Validate(kind, input)
					if err != nil {
						t.Fatal(err)
					}
					key, conflict := "event_id", "conflicting_event_id"
					if kind == "run" {
						key, conflict = "run_id", "conflicting_run_id"
					}
					id := m[key].(string)
					if old, exists := seen[id]; exists && !Equivalent(kind, old, m) {
						conflicts[conflict] = true
					}
					seen[id] = m
					for name, conflict := range map[string]string{"execution.context": "conflicting_context", "execution.ended": "conflicting_end"} {
						if m["name"] == name {
							attrs := comparable(kind, m)["attributes"]
							if old, exists := contexts[name]; exists && !EqualJSON(old, attrs) {
								conflicts[conflict] = true
							}
							contexts[name] = attrs
						}
					}
				}
				var actual []string
				for key := range conflicts {
					actual = append(actual, key)
				}
				sort.Strings(actual)
				if strings.Join(actual, ",") != strings.Join(tc.Conflicts, ",") {
					t.Fatalf("conflicts %v != %v", actual, tc.Conflicts)
				}
				return
			}
			value, err := c.Validate(tc.Kind, raw)
			if tc.Expected != "ok" {
				wantCode(t, err, tc.Expected)
				return
			}
			if err != nil {
				t.Fatal(err)
			}
			encoded, err := c.Encode(tc.Kind, value)
			if err != nil {
				t.Fatal(err)
			}
			roundtrip, err := c.Validate(tc.Kind, encoded)
			if err != nil || !Equivalent(tc.Kind, value, roundtrip) {
				t.Fatalf("round trip changed content: %v", err)
			}
		})
	}
}

func TestExactComparisonVectors(t *testing.T) {
	var vectors []struct {
		Left, Right string
		Equal       bool
	}
	if err := json.Unmarshal(fixture(t, "comparisons.json"), &vectors); err != nil {
		t.Fatal(err)
	}
	for _, vector := range vectors {
		a, err := Decode([]byte(vector.Left))
		if err != nil {
			t.Fatal(err)
		}
		b, err := Decode([]byte(vector.Right))
		if err != nil || EqualJSON(a, b) != vector.Equal {
			t.Fatalf("%s / %s: %v", vector.Left, vector.Right, err)
		}
	}
}

func TestJSONBoundaries(t *testing.T) {
	for _, raw := range []string{"1e309", "1e-999", "9223372036854775808", "-9223372036854775809"} {
		_, err := Decode([]byte(raw))
		wantCode(t, err, "number_out_of_range")
	}
	for _, raw := range []string{"5e-324", "0e-999", "0e999999999999999999999", "9007199254740993.0", "-0"} {
		if _, err := Decode([]byte(raw)); err != nil {
			t.Fatalf("%s: %v", raw, err)
		}
	}
	for _, raw := range []string{`{"a":1,"\u0061":2}`, `{"x":{"a":1,"a":2}}`} {
		_, err := Decode([]byte(raw))
		wantCode(t, err, "duplicate_key")
	}
	for _, tc := range []struct{ raw, code string }{
		{`"\ud800"`, "invalid_unicode"}, {`"\udc00"`, "invalid_unicode"},
		{`"\u0000"`, "invalid_unicode"}, {`{"\u0000":1}`, "invalid_unicode"},
		{`"` + strings.Repeat("é", 2049) + `"`, "limit_exceeded"},
		{`{"` + strings.Repeat("é", 65) + `":1}`, "limit_exceeded"},
		{strings.Repeat("[", 17) + "0" + strings.Repeat("]", 17), "limit_exceeded"},
		{`[` + strings.Repeat("0,", 128) + `0]`, "limit_exceeded"},
	} {
		_, err := Decode([]byte(tc.raw))
		wantCode(t, err, tc.code)
	}
	for _, raw := range []string{`"\ud83d\ude00"`, `"` + strings.Repeat("é", 2048) + `"`, strings.Repeat("[", 16) + "0" + strings.Repeat("]", 16)} {
		if _, err := Decode([]byte(raw)); err != nil {
			t.Fatal(err)
		}
	}
	raw := fixture(t, "signal.json")
	padded := append(bytes.Clone(raw), bytes.Repeat([]byte{' '}, MaxRecordBytes-len(raw))...)
	if _, err := codec(t).Validate("event", padded); err != nil {
		t.Fatal(err)
	}
	_, err := codec(t).Validate("event", append(padded, ' '))
	wantCode(t, err, "limit_exceeded")
}

func TestIntegerLexemesAndClockBoundaries(t *testing.T) {
	c := codec(t)
	for _, field := range []string{`"exit_code":1`, `"signal":1`, `"duration_ns":1`} {
		raw := bytes.Replace(fixture(t, "signal.json"), []byte(`"signal":15`), []byte(field), 1)
		if strings.Contains(field, "duration") {
			raw = bytes.Replace(raw, []byte(field), []byte(`"exit_code":0,`+field), 1)
		}
		_, err := c.Validate("event", bytes.Replace(raw, []byte(field), []byte(field+".0"), 1))
		wantCode(t, err, "schema_invalid")
	}
	for _, input := range []string{"2026-02-30T00:00:00Z", "2026-01-01T00:00:60Z", "2026-01-01T00:00:00+24:00", "2026-01-01T00:00:00+01:60", "0000-01-01T00:00:00Z", "2026-01-01T00:00:00-00:00"} {
		_, err := ParseTime(input)
		wantCode(t, err, "invalid_time")
	}
	a, _ := ParseTime("2026-01-01T00:00:00.000000001Z")
	b, _ := ParseTime("2026-01-01T08:00:00.000000001+08:00")
	cTime, _ := ParseTime("2026-01-01T00:00:00.000000002Z")
	if !a.Equal(b) || a.Equal(cTime) {
		t.Fatal("nanosecond normalization")
	}
}

func TestAutomaticStartAndEncoding(t *testing.T) {
	c := codec(t)
	a, err := c.NewStart(nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	b, _ := c.NewStart(nil, nil)
	first, _ := c.Validate("event", a)
	second, _ := c.Validate("event", b)
	if first["execution_id"] == second["execution_id"] || first["event_id"] == second["event_id"] {
		t.Fatal("IDs reused")
	}
	attrs := first["attributes"].(map[string]any)
	if attrs["task"] != nil || attrs["engine"] != nil {
		t.Fatal("inferred entry context")
	}
	for _, value := range []any{string([]byte{0xff}), map[string]any{string([]byte{0xff}): "value"}} {
		_, err := c.NewEvent(first["execution_id"].(string), "test.event", map[string]any{"value": value})
		wantCode(t, err, "invalid_unicode")
	}
	_, err = c.NewEvent(first["execution_id"].(string), "test.event", map[string]any{"n": math.NaN()})
	wantCode(t, err, "invalid_number")
}

func TestEmbeddedResourceOutsideSourceDirectory(t *testing.T) {
	want, err := os.ReadFile(filepath.Join(specDir(), "contract.schema.json"))
	if err != nil || !bytes.Equal(want, contract) {
		t.Fatal("embedded resource differs")
	}
	input := fixture(t, "signal.json")
	before, _ := os.Getwd()
	if err := os.Chdir(t.TempDir()); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = os.Chdir(before) })
	if _, err := codec(t).Validate("event", input); err != nil {
		t.Fatal(err)
	}
}
