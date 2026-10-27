// Package execution implements the independent Execution 2.0.0 file contract.
// It has no dependency on legacy registration, contexts, events or spool.
package execution

import (
	"bytes"
	"crypto/rand"
	"encoding/json"
	"errors"
	"fmt"
	"math/big"
	"regexp"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/santhosh-tekuri/jsonschema/v5"
)

const FormatVersion = "2.0.0"
const LayoutVersion = 2

type Error struct{ Code string }

func (e *Error) Error() string { return e.Code }
func bad(code string) error    { return &Error{Code: code} }
func Code(err error) string {
	var problem *Error
	if errors.As(err, &problem) {
		return problem.Code
	}
	return "io_unavailable"
}

type Codec struct{ schemas map[string]*jsonschema.Schema }

func NewCodec() (*Codec, error) {
	schemas, err := loadSchemas()
	if err != nil {
		return nil, err
	}
	return &Codec{schemas: schemas}, nil
}

func (c *Codec) Validate(kind string, raw []byte) (map[string]any, error) {
	schema, ok := c.schemas[kind]
	if !ok {
		return nil, bad("unknown_kind")
	}
	value, err := Decode(raw)
	if err != nil {
		return nil, err
	}
	if kind == "event" && bytes.ContainsAny(raw, "\r\n") {
		return nil, bad("invalid_json")
	}
	m, ok := value.(map[string]any)
	if !ok {
		return nil, bad("schema_invalid")
	}
	if m["format_version"] != FormatVersion {
		return nil, bad("unsupported_version")
	}
	if kind == "root" && !integerLexeme(m["layout_version"]) {
		return nil, bad("schema_invalid")
	}
	attrs, _ := m["attributes"].(map[string]any)
	if kind == "event" && (m["name"] == "execution.ended" || m["name"] == "stage.ended") {
		for _, key := range []string{"exit_code", "signal", "duration_ns"} {
			if v, exists := attrs[key]; exists && !integerLexeme(v) {
				return nil, bad("schema_invalid")
			}
		}
	}
	if schema.Validate(value) != nil {
		return nil, bad("schema_invalid")
	}
	switch kind {
	case "root":
		if _, err := partitionZone(m["partition_timezone"].(string)); err != nil {
			return nil, err
		}
	case "run":
		if _, err := ParseTime(m["created_at"].(string)); err != nil {
			return nil, err
		}
		if m["cycle"] != nil {
			if _, err := ParseTime(m["cycle"].(string)); err != nil {
				return nil, err
			}
		}
	case "event":
		if _, err := ParseTime(m["occurred_at"].(string)); err != nil {
			return nil, err
		}
		if m["name"] == "execution.context" && attrs["cycle"] != nil {
			if _, err := ParseTime(attrs["cycle"].(string)); err != nil {
				return nil, err
			}
		}
	}
	return m, nil
}

func integerLexeme(value any) bool {
	n, ok := value.(json.Number)
	return ok && !strings.ContainsAny(string(n), ".eE")
}

// Encode accepts a JSON value tree and returns one validated object without LF.
// Numbers may be json.Number (preferred for exact decimals) or native Go numbers.
func (c *Codec) Encode(kind string, value map[string]any) ([]byte, error) {
	budget := 2 * MaxRecordBytes
	if err := encodable(value, 0, &budget); err != nil {
		return nil, err
	}
	raw, err := json.Marshal(value)
	if err != nil {
		return nil, bad("invalid_number")
	}
	if _, err := c.Validate(kind, raw); err != nil {
		return nil, err
	}
	return raw, nil
}

func encodable(value any, depth int, budget *int) error {
	*budget -= 1
	if *budget < 0 {
		return bad("limit_exceeded")
	}
	switch x := value.(type) {
	case string:
		*budget -= len(x)
		if *budget < 0 {
			return bad("limit_exceeded")
		}
		if !utf8.ValidString(x) {
			return bad("invalid_unicode")
		}
		return textBoundary(x, MaxTextBytes)
	case map[string]any:
		if depth >= MaxDepth || len(x) > MaxItems {
			return bad("limit_exceeded")
		}
		for key, child := range x {
			if err := encodable(key, 0, budget); err != nil {
				return err
			}
			if len(key) > MaxKeyBytes {
				return bad("limit_exceeded")
			}
			if err := encodable(child, depth+1, budget); err != nil {
				return err
			}
		}
	case []any:
		if depth >= MaxDepth || len(x) > MaxItems {
			return bad("limit_exceeded")
		}
		for _, child := range x {
			if err := encodable(child, depth+1, budget); err != nil {
				return err
			}
		}
	case json.Number:
		*budget -= len(x)
		if *budget < 0 {
			return bad("limit_exceeded")
		}
	case nil, bool, int, int64, uint64, float64, float32:
	default:
		return bad("schema_invalid")
	}
	return nil
}

var timePattern = regexp.MustCompile(`^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?(Z|[+-][0-9]{2}:[0-9]{2})$`)
var uuidPattern = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)

func ParseTime(value string) (time.Time, error) {
	if !timePattern.MatchString(value) || strings.HasPrefix(value, "0000") || strings.HasSuffix(value, "-00:00") {
		return time.Time{}, bad("invalid_time")
	}
	if !strings.HasSuffix(value, "Z") {
		offset := value[len(value)-6:]
		if offset[1:3] > "23" || offset[4:6] > "59" {
			return time.Time{}, bad("invalid_time")
		}
	}
	t, err := time.Parse(time.RFC3339Nano, value)
	if err != nil {
		return time.Time{}, bad("invalid_time")
	}
	return t, nil
}

func NewID() (string, error) {
	var id [16]byte
	if _, err := rand.Read(id[:]); err != nil {
		return "", bad("identity_unavailable")
	}
	id[6] = id[6]&0x0f | 0x40
	id[8] = id[8]&0x3f | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", id[:4], id[4:6], id[6:8], id[8:10], id[10:]), nil
}

// NewStart allocates an execution identity at the public entry, without Run/cycle.
func (c *Codec) NewStart(task *string, engine map[string]any) ([]byte, error) {
	entry := time.Now().UTC()
	id, err := NewID()
	if err != nil {
		return nil, err
	}
	var knownTask any
	if task != nil {
		knownTask = *task
	}
	return c.newEventAt(id, "execution.started", map[string]any{"task": knownTask, "engine": engine}, entry)
}

// NewEvent is for a new fact. Retransmit its returned bytes, never reallocate IDs.
func (c *Codec) NewEvent(executionID, name string, attributes map[string]any) ([]byte, error) {
	return c.newEventAt(executionID, name, attributes, time.Now().UTC())
}

func (c *Codec) newEventAt(executionID, name string, attributes map[string]any, occurred time.Time) ([]byte, error) {
	id, err := NewID()
	if err != nil {
		return nil, err
	}
	if attributes == nil {
		attributes = map[string]any{}
	}
	return c.Encode("event", map[string]any{
		"format_version": FormatVersion, "event_id": id, "execution_id": executionID,
		"name": name, "occurred_at": occurred.Format(time.RFC3339Nano), "attributes": attributes,
	})
}

// EqualJSON compares decoded JSON exactly, with decimal numeric equality and no NFC.
func EqualJSON(a, b any) bool {
	switch x := a.(type) {
	case json.Number:
		y, ok := b.(json.Number)
		if !ok {
			return false
		}
		left, right := rational(x), rational(y)
		return left != nil && right != nil && left.Cmp(right) == 0
	case map[string]any:
		y, ok := b.(map[string]any)
		if !ok || len(x) != len(y) {
			return false
		}
		for key, child := range x {
			other, exists := y[key]
			if !exists || !EqualJSON(child, other) {
				return false
			}
		}
		return true
	case []any:
		y, ok := b.([]any)
		if !ok || len(x) != len(y) {
			return false
		}
		for i := range x {
			if !EqualJSON(x[i], y[i]) {
				return false
			}
		}
		return true
	case string:
		y, ok := b.(string)
		return ok && x == y
	case bool:
		y, ok := b.(bool)
		return ok && x == y
	case nil:
		return b == nil
	}
	return false
}

func rational(n json.Number) *big.Rat {
	// Zero with an arbitrary exponent needs no big integer exponentiation.
	mantissa := strings.SplitN(strings.ToLower(string(n)), "e", 2)[0]
	if !strings.ContainsAny(mantissa, "123456789") {
		return new(big.Rat)
	}
	r, _ := new(big.Rat).SetString(string(n))
	return r
}

// Equivalent compares validated records, normalizing only defined time fields.
func Equivalent(kind string, a, b map[string]any) bool {
	return EqualJSON(comparable(kind, a), comparable(kind, b))
}

func comparable(kind string, m map[string]any) map[string]any {
	out := make(map[string]any, len(m))
	for key, value := range m {
		out[key] = value
	}
	keys := []string{"occurred_at"}
	if kind == "run" {
		keys = []string{"created_at", "cycle"}
	}
	for _, key := range keys {
		if s, ok := out[key].(string); ok {
			t, _ := ParseTime(s)
			out[key] = t.UTC().Format(time.RFC3339Nano)
		}
	}
	if kind == "event" && m["name"] == "execution.context" {
		attrs := make(map[string]any)
		for key, value := range m["attributes"].(map[string]any) {
			attrs[key] = value
		}
		if s, ok := attrs["cycle"].(string); ok {
			t, _ := ParseTime(s)
			attrs["cycle"] = t.UTC().Format(time.RFC3339Nano)
		}
		out["attributes"] = attrs
	}
	return out
}
