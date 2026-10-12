package event

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/cemc-oper/sledra/recorder/internal/speccontract"
)

func fixture(t *testing.T, name string) map[string]any {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("..", "..", "spec", "fixtures", "1.0.0", name))
	if err != nil {
		t.Fatal(err)
	}
	v, err := speccontract.Decode(b)
	if err != nil {
		t.Fatal(err)
	}
	return v.(map[string]any)
}

func sample(t *testing.T) map[string]any {
	t.Helper()
	return fixture(t, "valid-bundle.json")["events"].([]any)[2].(map[string]any)
}
func wire(t *testing.T, m map[string]any) []byte {
	t.Helper()
	b, err := json.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	return b
}
func code(t *testing.T, err error, want string) {
	t.Helper()
	var e *Error
	if !errors.As(err, &e) || e.Code != want {
		t.Fatalf("got %v, want %s", err, want)
	}
}

func TestFixturesAndEmbeddedContract(t *testing.T) {
	for _, name := range []string{"event.schema.json", "event-types.json"} {
		want, err := os.ReadFile(filepath.Join("..", "..", "spec", "1.0.0", name))
		if err != nil {
			t.Fatal(err)
		}
		got, err := contracts.ReadFile("contract/" + name)
		if err != nil {
			t.Fatal(err)
		}
		if !bytes.Equal(got, want) {
			t.Fatalf("embedded %s differs from public spec", name)
		}
	}
	for _, name := range []string{"valid-bundle.json", "valid-future-extension.json"} {
		b := fixture(t, name)
		for _, raw := range b["events"].([]any) {
			m := raw.(map[string]any)
			role := m["producer"].(map[string]any)["role"].(string)
			c, err := New(role)
			if err != nil {
				t.Fatal(err)
			}
			got, err := c.Validate(wire(t, m))
			if err != nil {
				t.Fatalf("%s %s: %v", name, m["event_type"], err)
			}
			canon, err := speccontract.Canonical(m)
			if err != nil {
				t.Fatal(err)
			}
			if !bytes.Equal(got.Bytes, append(canon, '\n')) {
				t.Fatalf("canonical mismatch: %s", m["event_type"])
			}
			if got.Opaque != (m["event_type"] == "future.metric_recorded") {
				t.Fatalf("opaque mismatch: %s", m["event_type"])
			}
		}
	}
}

func TestTypedEncodeAndStableID(t *testing.T) {
	id, err := NewID()
	if err != nil || !uuid.MatchString(id) {
		t.Fatalf("id %q %v", id, err)
	}
	m := sample(t)
	m["event_id"] = id
	b := wire(t, m)
	var e Envelope
	if err := json.Unmarshal(b, &e); err != nil {
		t.Fatal(err)
	}
	c, err := New("process_owner")
	if err != nil {
		t.Fatal(err)
	}
	one, err := c.Encode(e)
	if err != nil {
		t.Fatal(err)
	}
	two, err := c.Encode(e)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(one.Bytes, two.Bytes) {
		t.Fatal("retransmission changed bytes")
	}
	if bytes.Contains(one.Bytes, []byte("\\u003c")) {
		t.Fatal("unexpected ASCII escaping")
	}
}

func TestNegativeCases(t *testing.T) {
	c, err := New("process_owner")
	if err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name string
		edit func(map[string]any)
		want string
	}{
		{"role", func(m map[string]any) { m["producer"].(map[string]any)["role"] = "registry" }, "unauthorized_event"},
		{"source", func(m map[string]any) { m["source"].(map[string]any)["kind"] = "observed" }, "unauthorized_event"},
		{"missing association", func(m map[string]any) { delete(m["association"].(map[string]any), "attempt_id") }, "parent_mismatch"},
		{"missing payload", func(m map[string]any) { delete(m["payload"].(map[string]any), "invocation_id") }, "schema_invalid"},
		{"secret extension", func(m map[string]any) {
			m["extensions"] = map[string]any{"vendor.future": map[string]any{"password": "hidden"}}
		}, "sensitive_field"},
		{"bad time", func(m map[string]any) { m["recorded_at"] = "2026-10-10T03:12:14" }, "schema_invalid"},
		{"reserved", func(m map[string]any) { m["event_type"] = "task.success" }, "semantic_invalid"},
		{"major", func(m map[string]any) { m["schema_version"] = "2.0.0" }, "unsupported_major"},
		{"float", func(m map[string]any) {
			m["extensions"] = map[string]any{"vendor.future": map[string]any{"measure": 1.5}}
		}, "semantic_invalid"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			m := sample(t)
			tc.edit(m)
			_, err := c.Validate(wire(t, m))
			code(t, err, tc.want)
		})
	}
	base := wire(t, sample(t))
	_, err = c.Validate(bytes.Replace(base, []byte(`"kind":"event"`), []byte(`"kind":"event","kind":"event"`), 1))
	code(t, err, "duplicate_key")
	_, err = c.Validate(append(base, 0xff))
	code(t, err, "schema_invalid")
	_, err = c.Validate(bytes.Repeat([]byte("a"), MaxEventBytes+1))
	code(t, err, "limit_exceeded")
	_, err = c.Validate(bytes.Replace(base, []byte(`"invocation_id":"invocation-a"`), []byte(`"invocation_id":"/home/user/secret"`), 1))
	code(t, err, "sensitive_field")
}

func TestUnknownExtensionAndNoNetwork(t *testing.T) {
	t.Setenv("GOPROXY", "off")
	c, err := New("process_owner")
	if err != nil {
		t.Fatal(err)
	}
	m := sample(t)
	m["event_type"] = "future.metric_recorded"
	m["extensions"] = map[string]any{"vendor.future": map[string]any{"nested": "é", "count": 1}}
	r, err := c.Validate(wire(t, m))
	if err != nil {
		t.Fatal(err)
	}
	if !r.Opaque || !strings.Contains(string(r.Bytes), `"vendor.future"`) {
		t.Fatal("unknown extension lost")
	}
}

func FuzzValidate(f *testing.F) {
	b, _ := os.ReadFile(filepath.Join("..", "..", "spec", "fixtures", "1.0.0", "valid-bundle.json"))
	f.Add(b)
	f.Add([]byte(`{"schema_version":"1.0.0","kind":"event"}`))
	f.Add([]byte(`{"x":"\ud800"}`))
	c, err := New("process_owner")
	if err != nil {
		f.Fatal(err)
	}
	f.Fuzz(func(t *testing.T, data []byte) { _, _ = c.Validate(data) })
}
