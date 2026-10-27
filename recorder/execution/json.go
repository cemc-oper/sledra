package execution

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"math"
	"strconv"
	"strings"
	"unicode/utf8"
)

const (
	MaxRecordBytes = 65536
	MaxTextBytes   = 4096
	MaxKeyBytes    = 128
	MaxDepth       = 16
	MaxItems       = 128
)

// Decode preserves number lexemes and rejects invalid Unicode and duplicate keys.
// No legacy canonicalization, normalization or identity machinery is used.
func Decode(raw []byte) (any, error) {
	if len(raw) > MaxRecordBytes {
		return nil, bad("limit_exceeded")
	}
	if !utf8.Valid(raw) {
		return nil, bad("invalid_unicode")
	}
	if err := escapes(raw); err != nil {
		return nil, err
	}
	d := json.NewDecoder(bytes.NewReader(raw))
	d.UseNumber()
	value, err := decodeValue(d, 0)
	if err != nil {
		return nil, err
	}
	if _, err := d.Token(); !errors.Is(err, io.EOF) {
		return nil, bad("invalid_json")
	}
	return value, nil
}

// encoding/json substitutes U+FFFD for isolated surrogate escapes, so check first.
func escapes(raw []byte) error {
	inString := false
	for i := 0; i < len(raw); i++ {
		if raw[i] == '"' {
			inString = !inString
		} else if inString && raw[i] == '\\' {
			i++
			if i >= len(raw) {
				return bad("invalid_json")
			}
			if raw[i] != 'u' {
				continue
			}
			if i+4 >= len(raw) {
				return bad("invalid_json")
			}
			first, err := strconv.ParseUint(string(raw[i+1:i+5]), 16, 16)
			if err != nil {
				return bad("invalid_json")
			}
			i += 4
			if first >= 0xdc00 && first <= 0xdfff {
				return bad("invalid_unicode")
			}
			if first >= 0xd800 && first <= 0xdbff {
				if i+6 >= len(raw) || raw[i+1] != '\\' || raw[i+2] != 'u' {
					return bad("invalid_unicode")
				}
				second, err := strconv.ParseUint(string(raw[i+3:i+7]), 16, 16)
				if err != nil || second < 0xdc00 || second > 0xdfff {
					return bad("invalid_unicode")
				}
				i += 6
			}
		}
	}
	return nil
}

func decodeValue(d *json.Decoder, depth int) (any, error) {
	token, err := d.Token()
	if err != nil {
		// These non-JSON constants have a distinct contract diagnostic.
		remaining, _ := io.ReadAll(d.Buffered())
		trimmed := bytes.TrimSpace(remaining)
		if len(trimmed) > 0 && (trimmed[0] == ':' || trimmed[0] == ',') {
			trimmed = bytes.TrimSpace(trimmed[1:])
		}
		for _, constant := range []string{"NaN", "Infinity", "-Infinity"} {
			if bytes.HasPrefix(trimmed, []byte(constant)) {
				return nil, bad("invalid_number")
			}
		}
		return nil, bad("invalid_json")
	}
	switch value := token.(type) {
	case json.Delim:
		if depth >= MaxDepth {
			return nil, bad("limit_exceeded")
		}
		if value == '{' {
			out := map[string]any{}
			for d.More() {
				keyToken, err := d.Token()
				if err != nil {
					return nil, bad("invalid_json")
				}
				key, ok := keyToken.(string)
				if !ok {
					return nil, bad("invalid_json")
				}
				if err := textBoundary(key, MaxKeyBytes); err != nil {
					return nil, err
				}
				if _, exists := out[key]; exists {
					return nil, bad("duplicate_key")
				}
				if len(out) >= MaxItems {
					return nil, bad("limit_exceeded")
				}
				child, err := decodeValue(d, depth+1)
				if err != nil {
					return nil, err
				}
				out[key] = child
			}
			if end, err := d.Token(); err != nil || end != json.Delim('}') {
				return nil, bad("invalid_json")
			}
			return out, nil
		}
		if value == '[' {
			out := []any{}
			for d.More() {
				if len(out) >= MaxItems {
					return nil, bad("limit_exceeded")
				}
				child, err := decodeValue(d, depth+1)
				if err != nil {
					return nil, err
				}
				out = append(out, child)
			}
			if end, err := d.Token(); err != nil || end != json.Delim(']') {
				return nil, bad("invalid_json")
			}
			return out, nil
		}
		return nil, bad("invalid_json")
	case string:
		return value, textBoundary(value, MaxTextBytes)
	case json.Number:
		if !strings.ContainsAny(string(value), ".eE") {
			if _, err := value.Int64(); err != nil {
				return nil, bad("number_out_of_range")
			}
		} else {
			f, err := value.Float64()
			mantissa := strings.SplitN(strings.ToLower(string(value)), "e", 2)[0]
			nonzero := strings.ContainsAny(mantissa, "123456789")
			if err != nil || math.IsInf(f, 0) || math.IsNaN(f) || (nonzero && f == 0) {
				return nil, bad("number_out_of_range")
			}
		}
		return value, nil
	default:
		return token, nil
	}
}

func textBoundary(s string, limit int) error {
	if strings.ContainsRune(s, 0) {
		return bad("invalid_unicode")
	}
	if len(s) > limit {
		return bad("limit_exceeded")
	}
	return nil
}
