package execution

import (
	"bytes"
	"encoding/json"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
)

func TestChildSharedControlPreparedAcceptanceRejectionVectors(t *testing.T) {
	cases := filepath.Join(specDir(), "../execution-child-events/cases")
	var vectors []struct {
		ID, Kind, Base, Expected, Prefix, Suffix, Transform string
		SuffixHex                                           string `json:"suffix_hex"`
		Set                                                 map[string]json.RawMessage
		Remove                                              []string
		Fields                                              map[string]string
		PadTo                                               int `json:"pad_to"`
	}
	raw, err := os.ReadFile(filepath.Join(cases, "vectors.json"))
	if err != nil {
		t.Fatal(err)
	}
	if err = json.Unmarshal(raw, &vectors); err != nil {
		t.Fatal(err)
	}
	tested := 0
	for _, v := range vectors {
		if v.Kind == "receipt" {
			continue
		}
		t.Run(v.ID, func(t *testing.T) {
			raw, err := os.ReadFile(filepath.Join(cases, v.Base))
			if err != nil {
				t.Fatal(err)
			}
			if len(v.Set) > 0 || len(v.Remove) > 0 {
				var value map[string]json.RawMessage
				if err = json.Unmarshal(raw, &value); err != nil {
					t.Fatal(err)
				}
				for path, replacement := range v.Set {
					keys := strings.Split(path, "/")
					if len(keys) == 1 {
						value[keys[0]] = replacement
					} else {
						value[keys[0]] = childVectorPatch(t, value[keys[0]], keys[1:], replacement)
					}
				}
				for _, k := range v.Remove {
					delete(value, k)
				}
				raw, err = json.Marshal(value)
				if err != nil {
					t.Fatal(err)
				}
			}
			if len(v.Fields) > 0 {
				fields := strings.Split(strings.TrimSuffix(string(raw), "\n"), "\t")
				for key, value := range v.Fields {
					i, _ := strconv.Atoi(key)
					fields[i] = value
				}
				raw = []byte(strings.Join(fields, "\t") + "\n")
			}
			if v.Prefix != "" {
				raw = append([]byte(v.Prefix), raw[1:]...)
			}
			raw = append(raw, []byte(v.Suffix)...)
			if v.SuffixHex != "" {
				for i := 0; i < len(v.SuffixHex); i += 2 {
					b, _ := strconv.ParseUint(v.SuffixHex[i:i+2], 16, 8)
					raw = append(raw, byte(b))
				}
			}
			if v.PadTo > len(raw) {
				raw = append(raw, bytes.Repeat([]byte{' '}, v.PadTo-len(raw))...)
			}
			switch v.Transform {
			case "no_lf":
				raw = raw[:len(raw)-1]
			case "extra_lf":
				raw = append(raw, '\n')
			case "crlf":
				raw = append(raw[:len(raw)-1], '\r', '\n')
			case "nul":
				raw = append(raw[:len(raw)-1], 0, '\n')
			case "extra_field":
				raw = append(raw[:len(raw)-1], []byte("\textra\n")...)
			}
			if v.Kind == "prepared" {
				_, err = ParseChildPrepared(raw)
			} else {
				err = ValidateChildControl(v.Kind, raw, nil)
			}
			if (err == nil) != (v.Expected == "ok") {
				t.Fatalf("expected %s, got %v", v.Expected, err)
			}
		})
		tested++
	}
	if tested < 75 {
		t.Fatalf("only %d vectors", tested)
	}
}

func childVectorPatch(t *testing.T, raw json.RawMessage, path []string, value json.RawMessage) json.RawMessage {
	t.Helper()
	var object map[string]json.RawMessage
	if err := json.Unmarshal(raw, &object); err != nil {
		t.Fatal(err)
	}
	if len(path) == 1 {
		object[path[0]] = value
	} else {
		object[path[0]] = childVectorPatch(t, object[path[0]], path[1:], value)
	}
	result, err := json.Marshal(object)
	if err != nil {
		t.Fatal(err)
	}
	return result
}
