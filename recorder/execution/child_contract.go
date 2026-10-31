package execution

import (
	"bytes"
	"crypto/rand"
	"crypto/sha256"
	_ "embed"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"io"
	"math"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"unicode"
	"unicode/utf8"

	"github.com/santhosh-tekuri/jsonschema/v5"
)

const (
	ChildInterface       = "CE-1"
	ChildProtocol        = "execution-child-events"
	ChildVersion         = 1
	ChildPreparedLimit   = 66048
	ChildDescriptorLimit = 16384
	ChildStateLimit      = 524288
	ChildClosingLimit    = 131072
	ChildReceiptLimit    = 4096
	ChildArgvLimit       = 131072
)

//go:embed childcontract/contract.schema.json
var childContract []byte
var childSchemas map[string]*jsonschema.Schema
var childSchemaOnce sync.Once
var childSchemaErr error
var childHex = regexp.MustCompile(`^[0-9a-f]{64}$`)
var childUnsigned = regexp.MustCompile(`^(0|[1-9][0-9]{0,19})$`)
var childNames = map[string]string{"start": "execution.started", "context": "execution.context", "event": "", "stage-start": "stage.started", "stage-end": "stage.ended", "end": "execution.ended"}

// ChildPrepared is an internal, single-use fact. Only start carries session/key.
type ChildPrepared struct {
	RequestID, ExecutionID, EventID, Auxiliary, SessionID, OwnerKey, Digest string
	Kind                                                                    string
	Record                                                                  []byte
}

func childDigest(raw []byte) string { h := sha256.Sum256(raw); return hex.EncodeToString(h[:]) }

func NewChildPrepared(raw []byte, auxiliary string) (*ChildPrepared, error) {
	c, err := NewCodec()
	if err != nil {
		return nil, err
	}
	e, err := c.Validate("event", raw)
	if err != nil {
		return nil, err
	}
	request, err := NewID()
	if err != nil {
		return nil, err
	}
	p := &ChildPrepared{RequestID: request, ExecutionID: e["execution_id"].(string), EventID: e["event_id"].(string), Auxiliary: auxiliary, SessionID: "-", OwnerKey: "-", Digest: childDigest(raw), Record: bytes.Clone(raw)}
	if e["name"] == "execution.started" {
		p.SessionID, err = NewID()
		if err != nil {
			return nil, err
		}
		var key [32]byte
		if _, err = rand.Read(key[:]); err != nil {
			return nil, bad("identity_unavailable")
		}
		p.OwnerKey = hex.EncodeToString(key[:])
	}
	return ParseChildPrepared(p.Bytes())
}

func (p *ChildPrepared) Bytes() []byte {
	return []byte(strings.Join([]string{"1", p.RequestID, p.ExecutionID, p.EventID, p.Auxiliary, p.SessionID, p.OwnerKey, p.Digest, string(p.Record)}, "\t") + "\n")
}

func ReadChildPrepared(input io.Reader) (*ChildPrepared, error) {
	raw, err := io.ReadAll(io.LimitReader(input, ChildPreparedLimit+1))
	if err != nil {
		return nil, bad("invalid_value")
	}
	return ParseChildPrepared(raw)
}
func ParseChildPrepared(raw []byte) (*ChildPrepared, error) {
	if len(raw) > ChildPreparedLimit {
		return nil, bad("limit_exceeded")
	}
	if len(raw) == 0 || raw[len(raw)-1] != '\n' || !utf8.Valid(raw) {
		return nil, bad("invalid_value")
	}
	for _, b := range raw[:len(raw)-1] {
		if b < 32 && b != '\t' || b == 127 {
			return nil, bad("invalid_value")
		}
	}
	fields := strings.Split(string(raw[:len(raw)-1]), "\t")
	if len(fields) != 9 || fields[0] != "1" {
		return nil, bad("invalid_value")
	}
	for _, f := range fields {
		if f == "" {
			return nil, bad("invalid_value")
		}
	}
	for _, id := range fields[1:4] {
		if !uuidPattern.MatchString(id) {
			return nil, bad("invalid_value")
		}
	}
	if !childHex.MatchString(fields[7]) {
		return nil, bad("invalid_value")
	}
	c, err := NewCodec()
	if err != nil {
		return nil, err
	}
	e, err := c.Validate("event", []byte(fields[8]))
	if err != nil {
		return nil, err
	}
	p := &ChildPrepared{RequestID: fields[1], ExecutionID: fields[2], EventID: fields[3], Auxiliary: fields[4], SessionID: fields[5], OwnerKey: fields[6], Digest: fields[7], Record: []byte(fields[8]), Kind: "event"}
	for kind, name := range childNames {
		if name == e["name"] {
			p.Kind = kind
		}
	}
	if e["execution_id"] != p.ExecutionID || e["event_id"] != p.EventID || p.Digest != childDigest(p.Record) {
		return nil, bad("identity_mismatch")
	}
	if err = childOperation(c, p); err != nil {
		return nil, err
	}
	if p.Kind == "start" {
		if !uuidPattern.MatchString(p.SessionID) || !childHex.MatchString(p.OwnerKey) || !strings.HasSuffix(p.Auxiliary, "Z") {
			return nil, bad("invalid_value")
		}
	} else if p.SessionID != "-" || p.OwnerKey != "-" {
		return nil, bad("role_denied")
	}
	return p, nil
}
func childOperation(c *Codec, p *ChildPrepared) error {
	e, err := c.Validate("event", p.Record)
	if err != nil {
		return err
	}
	if e["execution_id"] != p.ExecutionID || e["event_id"] != p.EventID || childDigest(p.Record) != p.Digest {
		return bad("identity_mismatch")
	}
	if p.Kind == "event" {
		if strings.HasPrefix(e["name"].(string), "execution.") || strings.HasPrefix(e["name"].(string), "stage.") {
			return bad("reserved_event_name")
		}
	} else if name, ok := childNames[p.Kind]; !ok || name != e["name"] {
		return bad("invalid_value")
	}
	aux := "-"
	if p.Kind == "start" {
		aux = e["occurred_at"].(string)
	}
	if p.Kind == "stage-start" {
		aux = e["attributes"].(map[string]any)["stage_id"].(string)
	}
	if aux != p.Auxiliary {
		return bad("identity_mismatch")
	}
	return nil
}

func ChildReference(ref string) (runtime, session, path string, err error) {
	if len(ref) > 2048 {
		return "", "", "", bad("limit_exceeded")
	}
	if !strings.HasPrefix(ref, "sledra-ce1:") {
		return "", "", "", bad("invalid_reference")
	}
	file := strings.TrimPrefix(ref, "sledra-ce1:")
	if err = childAbsolute(file, 2048); err != nil {
		return
	}
	path = filepath.Dir(file)
	runtime = filepath.Dir(path)
	session = filepath.Base(path)
	if filepath.Base(file) != "event-context.json" || !uuidPattern.MatchString(session) {
		err = bad("invalid_reference")
		return
	}
	err = childAbsolute(runtime, 1024)
	return
}
func childAbsolute(path string, limit int) error {
	if len(path) > limit {
		return bad("limit_exceeded")
	}
	if !utf8.ValidString(path) || !filepath.IsAbs(path) || strings.HasPrefix(path, "//") || filepath.Clean(path) != path {
		return bad("invalid_reference")
	}
	for _, r := range path {
		if unicode.IsControl(r) {
			return bad("invalid_reference")
		}
	}
	return nil
}

type ChildDescriptor struct {
	Protocol      string     `json:"protocol"`
	Version       int        `json:"version"`
	Interface     string     `json:"interface"`
	Role          string     `json:"role"`
	SessionID     string     `json:"session_id"`
	ExecutionID   string     `json:"execution_id"`
	EntryTime     string     `json:"entry_time"`
	UID           uint32     `json:"uid"`
	BootID        string     `json:"boot_id"`
	PIDNamespace  string     `json:"pid_namespace_inode"`
	OwnerPID      int        `json:"owner_pid"`
	OwnerStart    string     `json:"owner_start_ticks"`
	RuntimePath   string     `json:"runtime_path"`
	RuntimeDevice string     `json:"runtime_device"`
	RuntimeInode  string     `json:"runtime_inode"`
	SessionDevice string     `json:"session_device"`
	SessionInode  string     `json:"session_inode"`
	LockDevice    string     `json:"lock_device"`
	LockInode     string     `json:"lock_inode"`
	RootPath      string     `json:"root_path"`
	RootDevice    string     `json:"root_device"`
	RootInode     string     `json:"root_inode"`
	RootConfig    RootConfig `json:"root_config"`
}
type ChildStage struct {
	Name string `json:"name"`
	ID   string `json:"stage_id"`
}
type ChildPending struct {
	RequestID string `json:"request_id"`
	Kind      string `json:"kind"`
	EventID   string `json:"event_id"`
	Record    []byte `json:"record_base64"`
	Digest    string `json:"record_sha256"`
	Auxiliary string `json:"auxiliary"`
	Target    string `json:"target_phase"`
}
type ChildResult struct {
	RequestID string `json:"request_id"`
	EventID   string `json:"event_id"`
	Digest    string `json:"record_sha256"`
	Revision  int64  `json:"revision"`
	Result    string `json:"result"`
	Code      string `json:"code"`
	Phase     string `json:"phase"`
}
type ChildState struct {
	Protocol         string        `json:"protocol"`
	Version          int           `json:"version"`
	SessionID        string        `json:"session_id"`
	ExecutionID      string        `json:"execution_id"`
	DescriptorDigest string        `json:"descriptor_sha256"`
	OwnerDigest      string        `json:"owner_key_sha256"`
	Revision         int64         `json:"revision"`
	Phase            string        `json:"phase"`
	RecordDevice     *string       `json:"record_device"`
	RecordInode      *string       `json:"record_inode"`
	Context          []byte        `json:"context"`
	ActiveStage      *ChildStage   `json:"active_stage"`
	End              []byte        `json:"end"`
	Pending          *ChildPending `json:"pending"`
	Last             *ChildResult  `json:"last_result"`
}
type ChildClosing struct {
	Protocol  string `json:"protocol"`
	Version   int    `json:"version"`
	SessionID string `json:"session_id"`
	RequestID string `json:"request_id"`
	EventID   string `json:"event_id"`
	Digest    string `json:"record_sha256"`
	Record    []byte `json:"record_base64"`
}

type ChildReceipt struct {
	Status                           int
	Result, Code, RequestID, EventID string
	Revision                         *int64
	Phase, Stop, Auxiliary           string
}

func childReply(status int, code string, p *ChildPrepared) ChildReceipt {
	results := map[int]string{0: "committed", 20: "rejected", 21: "busy", 22: "closed", 23: "disabled", 24: "unknown", 25: "incompatible", 26: "unavailable"}
	r := ChildReceipt{Status: status, Result: results[status], Code: code, Phase: "unavailable", Stop: "none", Auxiliary: "-"}
	if status >= 23 {
		r.Stop = "local"
	}
	if p != nil {
		r.RequestID = p.RequestID
		r.EventID = p.EventID
	}
	return r
}
func ChildRejection(code string) ChildReceipt { return childReply(20, code, nil) }
func (r ChildReceipt) Bytes() []byte {
	dash := func(s string) string {
		if s == "" {
			return "-"
		}
		return s
	}
	revision := "-"
	if r.Revision != nil {
		revision = strconv.FormatInt(*r.Revision, 10)
	}
	return []byte(strings.Join([]string{"1", r.Result, r.Code, dash(r.RequestID), dash(r.EventID), revision, r.Phase, r.Stop, dash(r.Auxiliary)}, "\t") + "\n")
}

func loadChildSchemas() error {
	childSchemaOnce.Do(func() {
		compiler := jsonschema.NewCompiler()
		compiler.LoadURL = func(string) (io.ReadCloser, error) { return nil, bad("invalid_value") }
		if childSchemaErr = compiler.AddResource("child.json", bytes.NewReader(childContract)); childSchemaErr != nil {
			return
		}
		childSchemas = map[string]*jsonschema.Schema{}
		for _, kind := range []string{"descriptor", "state", "closing"} {
			childSchemas[kind], childSchemaErr = compiler.Compile("child.json#/$defs/" + kind)
			if childSchemaErr != nil {
				return
			}
		}
	})
	return childSchemaErr
}

// Control decoding has its own bounds: base64 caches exceed data text limits.
func decodeChildValue(d *json.Decoder, depth int) (any, error) {
	if depth > 16 {
		return nil, bad("limit_exceeded")
	}
	token, err := d.Token()
	if err != nil {
		return nil, bad("invalid_json")
	}
	switch v := token.(type) {
	case json.Delim:
		if v == '{' {
			m := map[string]any{}
			for d.More() {
				key, err := d.Token()
				if err != nil {
					return nil, bad("invalid_json")
				}
				k, ok := key.(string)
				if !ok {
					return nil, bad("invalid_json")
				}
				if _, exists := m[k]; exists {
					return nil, bad("duplicate_key")
				}
				if len(m) >= 128 {
					return nil, bad("limit_exceeded")
				}
				m[k], err = decodeChildValue(d, depth+1)
				if err != nil {
					return nil, err
				}
			}
			end, err := d.Token()
			if err != nil || end != json.Delim('}') {
				return nil, bad("invalid_json")
			}
			return m, nil
		}
		if v == '[' {
			a := []any{}
			for d.More() {
				if len(a) >= 128 {
					return nil, bad("limit_exceeded")
				}
				item, err := decodeChildValue(d, depth+1)
				if err != nil {
					return nil, err
				}
				a = append(a, item)
			}
			end, err := d.Token()
			if err != nil || end != json.Delim(']') {
				return nil, bad("invalid_json")
			}
			return a, nil
		}
		return nil, bad("invalid_json")
	case json.Number:
		if strings.ContainsAny(string(v), ".eE") {
			return nil, bad("schema_invalid")
		}
		return v, nil
	default:
		return v, nil
	}
}
func childRecord(encoded, executionID, name string) ([]byte, error) {
	raw, err := base64.StdEncoding.Strict().DecodeString(encoded)
	if err != nil || base64.StdEncoding.EncodeToString(raw) != encoded {
		return nil, bad("invalid_value")
	}
	c, err := NewCodec()
	if err != nil {
		return nil, err
	}
	event, err := c.Validate("event", raw)
	if err != nil {
		return nil, err
	}
	if executionID != "" && event["execution_id"] != executionID {
		return nil, bad("identity_mismatch")
	}
	if name != "" && event["name"] != name {
		return nil, bad("invalid_value")
	}
	return raw, nil
}

// ValidateChildControl validates closed control objects, never filesystem state.
func ValidateChildControl(kind string, raw []byte, target any) error {
	limits := map[string]int{"descriptor": ChildDescriptorLimit, "state": ChildStateLimit, "closing": ChildClosingLimit}
	limit, ok := limits[kind]
	if !ok {
		return bad("invalid_value")
	}
	if len(raw) > limit {
		return bad("limit_exceeded")
	}
	if !utf8.Valid(raw) {
		return bad("invalid_json")
	}
	if err := escapes(raw); err != nil {
		return err
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	value, err := decodeChildValue(d, 0)
	if err != nil {
		return err
	}
	if _, err = d.Token(); err != io.EOF {
		return bad("invalid_json")
	}
	if err = loadChildSchemas(); err != nil {
		return bad("invalid_value")
	}
	if childSchemas[kind].Validate(value) != nil {
		return bad("schema_invalid")
	}
	m := value.(map[string]any)
	switch kind {
	case "descriptor":
		for _, key := range []string{"runtime_path", "root_path"} {
			if err = childAbsolute(m[key].(string), 1024); err != nil {
				return err
			}
		}
		root, runtime := m["root_path"].(string), m["runtime_path"].(string)
		if childNested(root, runtime) || childNested(runtime, root) {
			return bad("invalid_reference")
		}
		c, _ := NewCodec()
		config, _ := json.Marshal(m["root_config"])
		if _, err = c.Validate("root", config); err != nil {
			return err
		}
		entry := m["entry_time"].(string)
		if _, err = ParseTime(entry); err != nil {
			return err
		}
		if !strings.HasSuffix(entry, "Z") {
			return bad("invalid_value")
		}
		for _, key := range []string{"pid_namespace_inode", "owner_start_ticks", "runtime_device", "runtime_inode", "session_device", "session_inode", "lock_device", "lock_inode", "root_device", "root_inode"} {
			if err = childUint(m[key].(string), !strings.HasSuffix(key, "device")); err != nil {
				return err
			}
		}
	case "closing":
		b, err := childRecord(m["record_base64"].(string), "", "execution.ended")
		if err != nil {
			return err
		}
		c, _ := NewCodec()
		e, _ := c.Validate("event", b)
		if e["event_id"] != m["event_id"] || childDigest(b) != m["record_sha256"] {
			return bad("identity_mismatch")
		}
	case "state":
		state := ChildState{}
		if err = json.Unmarshal(raw, &state); err != nil {
			return bad("invalid_value")
		}
		phase := state.Phase
		if (phase == "writing") != (state.Pending != nil) || ((phase == "active" || phase == "closed") && state.Revision < 2) {
			return bad("invalid_value")
		}
		if phase == "creating" && (state.Revision != 0 || state.Context != nil || state.End != nil || state.ActiveStage != nil || state.Last != nil || state.RecordInode != nil) {
			return bad("invalid_value")
		}
		if phase == "closed" && state.End == nil || phase == "active" && state.End != nil {
			return bad("invalid_value")
		}
		if (state.RecordDevice == nil) != (state.RecordInode == nil) {
			return bad("invalid_value")
		}
		uncreated := phase == "creating" || phase == "writing" && state.Pending.Kind == "start"
		if state.RecordInode == nil && !uncreated && phase != "disabled" {
			return bad("invalid_value")
		}
		for key, positive := range map[string]bool{"record_device": false, "record_inode": true} {
			if m[key] != nil {
				if err = childUint(m[key].(string), positive); err != nil {
					return err
				}
			}
		}
		for key, name := range map[string]string{"context": "execution.context", "end": "execution.ended"} {
			if m[key] != nil {
				if _, err = childRecord(m[key].(string), state.ExecutionID, name); err != nil {
					return err
				}
			}
		}
		if state.ActiveStage != nil {
			c, _ := NewCodec()
			_, err = c.Encode("event", map[string]any{"format_version": FormatVersion, "event_id": state.ExecutionID, "execution_id": state.ExecutionID, "occurred_at": "2026-10-30T00:00:00Z", "name": "stage.started", "attributes": map[string]any{"stage": state.ActiveStage.Name, "stage_id": state.ActiveStage.ID}})
			if err != nil {
				return err
			}
		}
		if state.Pending != nil {
			pending := state.Pending
			record, err := childRecord(m["pending"].(map[string]any)["record_base64"].(string), state.ExecutionID, "")
			if err != nil {
				return err
			}
			c, _ := NewCodec()
			p := &ChildPrepared{Kind: pending.Kind, ExecutionID: state.ExecutionID, EventID: pending.EventID, Record: record, Digest: pending.Digest, Auxiliary: pending.Auxiliary}
			if err = childOperation(c, p); err != nil {
				return err
			}
			target := "active"
			if pending.Kind == "end" {
				target = "closed"
			}
			if pending.Target != target {
				return bad("invalid_value")
			}
		}
		if state.Last != nil {
			last := state.Last
			if last.Revision > state.Revision || !(last.Result == "committed" && last.Code == "ok" || last.Result == "noop" && (last.Code == "same_context" || last.Code == "same_end")) {
				return bad("invalid_value")
			}
		}
	}
	if target != nil && json.Unmarshal(raw, target) != nil {
		return bad("invalid_value")
	}
	return nil
}
func childUint(value string, positive bool) error {
	if !childUnsigned.MatchString(value) {
		return bad("schema_invalid")
	}
	n, err := strconv.ParseUint(value, 10, 64)
	if err != nil || positive && n == 0 {
		return bad("schema_invalid")
	}
	return nil
}
func childNested(parent, path string) bool {
	return parent == path || strings.HasPrefix(path, strings.TrimSuffix(parent, "/")+"/")
}
func childStateBytes(s *ChildState) ([]byte, error) {
	raw, err := json.Marshal(s)
	if err == nil {
		err = ValidateChildControl("state", raw, nil)
	}
	return raw, err
}
func childAdvance(s *ChildState) error {
	if s.Revision == math.MaxInt64 {
		return bad("limit_exceeded")
	}
	s.Revision++
	return nil
}
func childPointer(v string) *string { return &v }
func childPending(p *ChildPrepared) *ChildPending {
	target := "active"
	if p.Kind == "end" {
		target = "closed"
	}
	return &ChildPending{p.RequestID, p.Kind, p.EventID, bytes.Clone(p.Record), p.Digest, p.Auxiliary, target}
}
func childKeyDigest(key string) (string, error) {
	if !childHex.MatchString(key) {
		return "", bad("role_denied")
	}
	raw, err := hex.DecodeString(key)
	if err != nil {
		return "", bad("role_denied")
	}
	return childDigest(raw), nil
}
