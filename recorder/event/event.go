// Package event encodes and validates Sledra events without network access.
package event

import (
	"bytes"
	"crypto/rand"
	"embed"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"regexp"
	"sort"
	"strings"
	"time"
	"unicode"

	"github.com/santhosh-tekuri/jsonschema/v5"
	"sledra.local/recorder/internal/speccontract"
)

//go:embed contract/*.json
var contracts embed.FS

const MaxEventBytes = 64 * 1024
const MaxPayloadBytes = 32 * 1024

type Error struct{ Code string }

func (e *Error) Error() string { return e.Code }
func bad(code string) error    { return &Error{Code: code} }

// Envelope is the typed producer input. Payload and extensions use JSON to
// preserve unknown 1.x fields and to reject duplicate keys before emission.
type Envelope struct {
	SchemaVersion string                     `json:"schema_version"`
	Kind          string                     `json:"kind"`
	EventID       string                     `json:"event_id"`
	EventType     string                     `json:"event_type"`
	Producer      Producer                   `json:"producer"`
	Source        Source                     `json:"source"`
	OccurredAt    *string                    `json:"occurred_at"`
	RecordedAt    string                     `json:"recorded_at"`
	TimeQuality   TimeQuality                `json:"time_quality"`
	Association   Association                `json:"association"`
	Payload       json.RawMessage            `json:"payload"`
	EvidenceRefs  []json.RawMessage          `json:"evidence_refs,omitempty"`
	Extensions    map[string]json.RawMessage `json:"extensions,omitempty"`
}
type Producer struct {
	Name    string `json:"name"`
	Version string `json:"version"`
	Role    string `json:"role"`
}
type Source struct {
	Kind           string `json:"kind"`
	SourceID       string `json:"source_id"`
	SessionID      string `json:"session_id"`
	Seq            *int64 `json:"seq,omitempty"`
	EngineInstance string `json:"engine_instance,omitempty"`
	BootID         string `json:"boot_id,omitempty"`
	ParserVersion  string `json:"parser_version,omitempty"`
}
type TimeQuality struct {
	Basis             string `json:"basis"`
	Reason            string `json:"reason,omitempty"`
	RawTimezone       string `json:"raw_timezone,omitempty"`
	PrecisionNS       *int64 `json:"precision_ns,omitempty"`
	ClockErrorBoundNS *int64 `json:"clock_error_bound_ns,omitempty"`
}
type Association struct {
	RunID          string `json:"run_id"`
	TaskInstanceID string `json:"task_instance_id,omitempty"`
	AttemptID      string `json:"attempt_id,omitempty"`
	JobRefID       string `json:"job_ref_id,omitempty"`
}

type Result struct {
	Bytes  []byte
	Opaque bool
}
type Codec struct {
	schema  *jsonschema.Schema
	payload map[string]*jsonschema.Schema
	catalog map[string]map[string]any
	role    string
}

// New loads only embedded, version-pinned contract resources. role is the
// installed producer role; callers cannot override it per event.
func New(role string) (*Codec, error) {
	data, err := contracts.ReadFile("contract/event.schema.json")
	if err != nil {
		return nil, err
	}
	compiler := jsonschema.NewCompiler()
	compiler.AssertFormat = true
	if err := compiler.AddResource("event.json", bytes.NewReader(data)); err != nil {
		return nil, err
	}
	schema, err := compiler.Compile("event.json")
	if err != nil {
		return nil, err
	}
	data, err = contracts.ReadFile("contract/event-types.json")
	if err != nil {
		return nil, err
	}
	var catalog map[string]map[string]any
	if err := json.Unmarshal(data, &catalog); err != nil {
		return nil, err
	}
	c := &Codec{schema: schema, payload: map[string]*jsonschema.Schema{}, catalog: catalog, role: role}
	for name, entry := range catalog {
		body, err := json.Marshal(entry["payload_schema"])
		if err != nil {
			return nil, err
		}
		compiler := jsonschema.NewCompiler()
		compiler.AssertFormat = true
		if err := compiler.AddResource("payload.json", bytes.NewReader(body)); err != nil {
			return nil, err
		}
		c.payload[name], err = compiler.Compile("payload.json")
		if err != nil {
			return nil, fmt.Errorf("compile payload %s: %w", name, err)
		}
	}
	return c, nil
}

// NewID generates a UUIDv4. Persist the returned event and reuse its ID and
// complete bytes for every retransmission; never call NewID on retry.
func NewID() (string, error) {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		return "", err
	}
	b[6] = b[6]&0x0f | 0x40
	b[8] = b[8]&0x3f | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", b[:4], b[4:6], b[6:8], b[8:10], b[10:]), nil
}

// Encode and Validate share exactly one validation path. The output is one
// canonical JSON line, ready for SLD-07's spool writer.
func (c *Codec) Encode(e Envelope) (Result, error) {
	data, err := json.Marshal(e)
	if err != nil {
		return Result{}, bad("schema_invalid")
	}
	return c.Validate(data)
}

func (c *Codec) Validate(data []byte) (Result, error) {
	if len(data) > MaxEventBytes {
		return Result{}, bad("limit_exceeded")
	}
	v, err := speccontract.Decode(data)
	if err != nil {
		return Result{}, classified(err)
	}
	m, ok := v.(map[string]any)
	if !ok {
		return Result{}, bad("schema_invalid")
	}
	version, _ := m["schema_version"].(string)
	if regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+$`).MatchString(version) && !strings.HasPrefix(version, "1.") {
		return Result{}, bad("unsupported_major")
	}
	if err := c.schema.Validate(v); err != nil {
		return Result{}, bad("schema_invalid")
	}
	if err := validateTime(m); err != nil {
		return Result{}, err
	}
	if err := validateID(m); err != nil {
		return Result{}, err
	}
	if !safeFields(v) {
		return Result{}, bad("sensitive_field")
	}
	payload := m["payload"]
	payloadBytes, err := speccontract.Canonical(payload)
	if err != nil {
		return Result{}, classified(err)
	}
	if len(payloadBytes) > MaxPayloadBytes {
		return Result{}, bad("limit_exceeded")
	}
	name := m["event_type"].(string)
	if name == "task.success" {
		return Result{}, bad("semantic_invalid")
	}
	producer := m["producer"].(map[string]any)
	if producer["role"] != c.role {
		return Result{}, bad("unauthorized_event")
	}
	entry, known := c.catalog[name]
	if known {
		source := m["source"].(map[string]any)
		role := producer["role"].(string)
		if role != c.role || !has(entry["roles"], role) || !has(entry["source_kinds"], source["kind"].(string)) {
			return Result{}, bad("unauthorized_event")
		}
		assoc := m["association"].(map[string]any)
		level := entry["association"].(string)
		if (level == "instance" || level == "attempt") && assoc["task_instance_id"] == nil {
			return Result{}, bad("parent_mismatch")
		}
		if level == "attempt" && assoc["attempt_id"] == nil {
			return Result{}, bad("parent_mismatch")
		}
		if assoc["attempt_id"] != nil && assoc["task_instance_id"] == nil {
			return Result{}, bad("parent_mismatch")
		}
		if entry["evidence_required"] == true && len(array(m["evidence_refs"])) == 0 {
			return Result{}, bad("semantic_invalid")
		}
		if err := c.payload[name].Validate(payload); err != nil {
			return Result{}, bad("schema_invalid")
		}
		if err := validateKnown(name, m); err != nil {
			return Result{}, err
		}
	}
	canonical, err := speccontract.Canonical(v)
	if err != nil {
		return Result{}, classified(err)
	}
	if len(canonical)+1 > MaxEventBytes {
		return Result{}, bad("limit_exceeded")
	}
	return Result{Bytes: append(canonical, '\n'), Opaque: !known}, nil
}

func classified(err error) error {
	var ce *speccontract.ContractError
	if errors.As(err, &ce) {
		return bad(ce.Code)
	}
	return bad("schema_invalid")
}
func has(v any, wanted string) bool {
	for _, x := range array(v) {
		if x == wanted {
			return true
		}
	}
	return false
}
func array(v any) []any { x, _ := v.([]any); return x }

var uuid = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)
var stable = regexp.MustCompile(`^evt_[0-9a-f]{64}$`)

func validateID(m map[string]any) error {
	id := m["event_id"].(string)
	if !uuid.MatchString(id) && !stable.MatchString(id) {
		return bad("semantic_invalid")
	}
	return nil
}
func validateTime(m map[string]any) error {
	q := m["time_quality"].(map[string]any)
	if (m["occurred_at"] == nil || q["basis"] == "unknown") && q["reason"] == nil {
		return bad("semantic_invalid")
	}
	for _, field := range []string{"occurred_at", "recorded_at"} {
		if s, ok := m[field].(string); ok && !timestamp(s) {
			return bad("schema_invalid")
		}
	}
	if p, ok := m["payload"].(map[string]any); ok {
		for _, field := range []string{"observation_time", "confirmed_at", "mtime_at", "deadline_at"} {
			if s, ok := p[field].(string); ok && !timestamp(s) {
				return bad("schema_invalid")
			}
		}
	}
	return nil
}

var timePattern = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$`)

func timestamp(s string) bool {
	if !timePattern.MatchString(s) {
		return false
	}
	_, err := time.Parse(time.RFC3339Nano, s)
	return err == nil
}

// A deliberately small denylist supplements the closed known payload schemas.
// Values in allowed fields must already be redacted by the producer.
var forbidden = regexp.MustCompile(`(?i)(password|passwd|secret|token|credential|private_key|authorization|environment|command_line|ftp_command)`)

func safeFields(v any) bool {
	switch x := v.(type) {
	case map[string]any:
		for k, child := range x {
			// Token digests in the public registration contract are permitted.
			if forbidden.MatchString(k) && !strings.HasSuffix(k, "_sha256") {
				return false
			}
			if !safeFields(child) {
				return false
			}
		}
	case []any:
		for _, child := range x {
			if !safeFields(child) {
				return false
			}
		}
	case string:
		if strings.HasPrefix(x, "/") || strings.HasPrefix(x, "~/") || strings.HasPrefix(x, "../") || strings.Contains(x, "/../") || strings.HasSuffix(x, "/..") || strings.HasPrefix(x, "file://") || strings.HasPrefix(x, "\\\\") || strings.Contains(x, "\\..\\") {
			return false
		}
		for _, r := range x {
			if unicode.IsControl(r) {
				return false
			}
		}
	}
	return true
}

func validateKnown(name string, m map[string]any) error {
	p := m["payload"].(map[string]any)
	if name == "attempt.exited" {
		_, code := p["exit_code"]
		_, signal := p["signal"]
		if (p["termination"] == "exited" && (!code || signal)) || (p["termination"] == "signaled" && (!signal || code)) {
			return bad("semantic_invalid")
		}
	}
	if name == "run.finish_declared" {
		ids := array(p["covered_attempt_ids"])
		values := make([]string, len(ids))
		for i, x := range ids {
			values[i], _ = x.(string)
		}
		if !sort.StringsAreSorted(values) {
			return bad("semantic_invalid")
		}
		digest, err := speccontract.SHA(ids)
		if err != nil || p["covered_attempts_sha256"] != digest {
			return bad("semantic_invalid")
		}
	}
	if name == "attempt.selection_declared" && !has(p["candidate_attempt_ids"], fmt.Sprint(p["selected_attempt_id"])) {
		return bad("semantic_invalid")
	}
	if name == "attempt.started" || name == "attempt.exited" || name == "stage.started" || name == "stage.ended" {
		if m["occurred_at"] == nil {
			return bad("semantic_invalid")
		}
	}
	for _, x := range array(m["evidence_refs"]) {
		e := x.(map[string]any)
		start, a := e["byte_start"].(json.Number)
		end, b := e["byte_end"].(json.Number)
		if a && b {
			s, _ := start.Int64()
			t, _ := end.Int64()
			if t < s {
				return bad("semantic_invalid")
			}
		}
	}
	return nil
}

// ReadBounded reads one event with an extra byte to classify oversize input.
func ReadBounded(r io.Reader) ([]byte, error) {
	b, err := io.ReadAll(io.LimitReader(r, MaxEventBytes+1))
	if err != nil {
		return nil, err
	}
	if len(b) > MaxEventBytes {
		return nil, bad("limit_exceeded")
	}
	return b, nil
}
