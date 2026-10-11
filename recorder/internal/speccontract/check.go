// Package speccontract is an offline contract test helper, not the recorder.
package speccontract

import (
	"bytes"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"reflect"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"

	"github.com/santhosh-tekuri/jsonschema/v5"
	"golang.org/x/text/unicode/norm"
)

type ContractError struct {
	Code   string `json:"code"`
	Detail string `json:"detail"`
}

func (e *ContractError) Error() string { return e.Detail }

func fail(code, detail string) error { return &ContractError{code, detail} }

func object(value any) (map[string]any, error) {
	v, ok := value.(map[string]any)
	if !ok {
		return nil, fail("schema_invalid", "expected object")
	}
	return v, nil
}

func str(value any) string { s, _ := value.(string); return s }
func arr(value any) []any  { a, _ := value.([]any); return a }

// Decode rejects repeated keys and invalid UTF-8 before encoding/json can discard evidence.
func Decode(data []byte) (any, error) {
	if !utf8.Valid(data) {
		return nil, fail("schema_invalid", "invalid UTF-8")
	}
	if err := checkSurrogateEscapes(data); err != nil {
		return nil, err
	}
	d := json.NewDecoder(bytes.NewReader(data))
	d.UseNumber()
	v, err := decodeValue(d)
	if err != nil {
		return nil, err
	}
	if _, err = d.Token(); !errors.Is(err, io.EOF) {
		return nil, fail("schema_invalid", "trailing JSON")
	}
	return v, nil
}

func checkSurrogateEscapes(data []byte) error {
	inString := false
	for i := 0; i < len(data); i++ {
		switch data[i] {
		case '"':
			inString = !inString
		case '\\':
			if !inString {
				continue
			}
			i++
			if i >= len(data) {
				return fail("schema_invalid", "incomplete JSON escape")
			}
			if data[i] != 'u' {
				continue
			}
			if i+4 >= len(data) {
				return fail("schema_invalid", "incomplete Unicode escape")
			}
			first, err := strconv.ParseUint(string(data[i+1:i+5]), 16, 16)
			if err != nil {
				return fail("schema_invalid", "invalid Unicode escape")
			}
			i += 4
			if first >= 0xDC00 && first <= 0xDFFF {
				return fail("schema_invalid", "unpaired Unicode surrogate")
			}
			if first >= 0xD800 && first <= 0xDBFF {
				if i+6 >= len(data) || data[i+1] != '\\' || data[i+2] != 'u' {
					return fail("schema_invalid", "unpaired Unicode surrogate")
				}
				second, err := strconv.ParseUint(string(data[i+3:i+7]), 16, 16)
				if err != nil || second < 0xDC00 || second > 0xDFFF {
					return fail("schema_invalid", "unpaired Unicode surrogate")
				}
				i += 6
			}
		}
	}
	return nil
}

func decodeValue(d *json.Decoder) (any, error) {
	token, err := d.Token()
	if err != nil {
		return nil, fail("schema_invalid", err.Error())
	}
	switch token {
	case json.Delim('{'):
		out := map[string]any{}
		for d.More() {
			keyToken, err := d.Token()
			if err != nil {
				return nil, fail("schema_invalid", err.Error())
			}
			key, ok := keyToken.(string)
			if !ok {
				return nil, fail("schema_invalid", "object key")
			}
			if _, exists := out[key]; exists {
				return nil, fail("duplicate_key", key)
			}
			value, err := decodeValue(d)
			if err != nil {
				return nil, err
			}
			out[key] = value
		}
		if _, err := d.Token(); err != nil {
			return nil, fail("schema_invalid", err.Error())
		}
		return out, nil
	case json.Delim('['):
		out := []any{}
		for d.More() {
			value, err := decodeValue(d)
			if err != nil {
				return nil, err
			}
			out = append(out, value)
		}
		if _, err := d.Token(); err != nil {
			return nil, fail("schema_invalid", err.Error())
		}
		return out, nil
	default:
		return token, nil
	}
}

func normalized(value any) (any, error) {
	switch v := value.(type) {
	case nil, bool:
		return v, nil
	case string:
		return norm.NFC.String(v), nil
	case json.Number:
		if strings.ContainsAny(string(v), ".eE") {
			return nil, fail("semantic_invalid", "floating-point values are forbidden")
		}
		number, err := strconv.ParseInt(string(v), 10, 64)
		if err != nil {
			return nil, fail("semantic_invalid", "integer exceeds signed 64-bit range")
		}
		return json.Number(strconv.FormatInt(number, 10)), nil
	case []any:
		out := make([]any, len(v))
		for i, item := range v {
			n, err := normalized(item)
			if err != nil {
				return nil, err
			}
			out[i] = n
		}
		return out, nil
	case map[string]any:
		out := map[string]any{}
		for key, item := range v {
			nk := norm.NFC.String(key)
			if _, exists := out[nk]; exists {
				return nil, fail("semantic_invalid", "keys collide after NFC normalization")
			}
			n, err := normalized(item)
			if err != nil {
				return nil, err
			}
			out[nk] = n
		}
		return out, nil
	default:
		return nil, fail("semantic_invalid", "unsupported JSON value")
	}
}

func Canonical(value any) ([]byte, error) {
	n, err := normalized(value)
	if err != nil {
		return nil, err
	}
	var out bytes.Buffer
	enc := json.NewEncoder(&out)
	enc.SetEscapeHTML(false)
	if err := enc.Encode(n); err != nil {
		return nil, err
	}
	return bytes.TrimSpace(out.Bytes()), nil
}

func SHA(value any) (string, error) {
	data, err := Canonical(value)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:]), nil
}

func LengthDigest(parts ...string) string {
	h := sha256.New()
	for _, part := range parts {
		b := []byte(norm.NFC.String(part))
		var length [8]byte
		binary.BigEndian.PutUint64(length[:], uint64(len(b)))
		h.Write(length[:])
		h.Write(b)
	}
	return hex.EncodeToString(h.Sum(nil))
}

func TaskInstanceID(run, task string, params map[string]any) (string, error) {
	data, err := Canonical(params)
	if err != nil {
		return "", err
	}
	return "ti_" + LengthDigest(run, task, string(data)), nil
}

func RegistryID(registry, kind, parent, token string) string {
	prefix := "run_"
	if kind == "attempt" {
		prefix = "att_"
	}
	return prefix + LengthDigest(registry, kind, parent, token)
}

func ContentSHA(doc map[string]any, excluded ...string) (string, error) {
	copy := map[string]any{}
	for key, value := range doc {
		copy[key] = value
	}
	for _, key := range excluded {
		delete(copy, key)
	}
	return SHA(copy)
}

type Checker struct {
	Schemas        map[string]*jsonschema.Schema
	PayloadSchemas map[string]*jsonschema.Schema
	Catalog        map[string]any
}

var kinds = []string{"definition_manifest", "run_context", "attempt_context", "job_ref", "artifact", "expectation", "event"}
var uuidV4 = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$`)

func NewChecker(specDir string) (*Checker, error) {
	checker := &Checker{Schemas: map[string]*jsonschema.Schema{}, PayloadSchemas: map[string]*jsonschema.Schema{}}
	for _, kind := range kinds {
		compiler := jsonschema.NewCompiler()
		compiler.AssertFormat = true
		path := filepath.Join(specDir, kind+".schema.json")
		schema, err := compiler.Compile(path)
		if err != nil {
			return nil, fmt.Errorf("compile %s: %w", path, err)
		}
		checker.Schemas[kind] = schema
	}
	data, err := os.ReadFile(filepath.Join(specDir, "event-types.json"))
	if err != nil {
		return nil, err
	}
	v, err := Decode(data)
	if err != nil {
		return nil, err
	}
	checker.Catalog, err = object(v)
	if err != nil {
		return nil, err
	}
	for name, raw := range checker.Catalog {
		entry, err := object(raw)
		if err != nil {
			return nil, err
		}
		payloadSchema, err := json.Marshal(entry["payload_schema"])
		if err != nil {
			return nil, err
		}
		compiler := jsonschema.NewCompiler()
		compiler.AssertFormat = true
		if err := compiler.AddResource("payload.json", bytes.NewReader(payloadSchema)); err != nil {
			return nil, err
		}
		compiled, err := compiler.Compile("payload.json")
		if err != nil {
			return nil, fmt.Errorf("compile payload %s: %w", name, err)
		}
		checker.PayloadSchemas[name] = compiled
	}
	return checker, nil
}

func (c *Checker) structure(doc any, kind string) error {
	m, err := object(doc)
	if err != nil {
		return err
	}
	version := str(m["schema_version"])
	if regexp.MustCompile(`^[0-9]+\.[0-9]+\.[0-9]+$`).MatchString(version) && !strings.HasPrefix(version, "1.") {
		return fail("unsupported_major", version)
	}
	if err := c.Schemas[kind].Validate(doc); err != nil {
		return fail("schema_invalid", kind+": "+err.Error())
	}
	if err := checkTimestamps(doc); err != nil {
		return err
	}
	return nil
}

var timeKeys = map[string]bool{"cycle_time": true, "occurred_at": true, "recorded_at": true, "deadline_at": true, "observation_time": true, "confirmed_at": true, "mtime_at": true}
var timePattern = regexp.MustCompile(`^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})$`)

func checkTimestamps(value any) error {
	switch v := value.(type) {
	case map[string]any:
		for key, child := range v {
			if key == "extensions" || key == "payload" {
				continue
			}
			if timeKeys[key] && child != nil {
				s := str(child)
				if !timePattern.MatchString(s) {
					return fail("schema_invalid", "timestamp needs RFC3339 offset: "+key)
				}
				if _, err := time.Parse(time.RFC3339Nano, s); err != nil {
					return fail("schema_invalid", "invalid timestamp: "+key)
				}
			}
			if err := checkTimestamps(child); err != nil {
				return err
			}
		}
	case []any:
		for _, child := range v {
			if err := checkTimestamps(child); err != nil {
				return err
			}
		}
	}
	return nil
}

func contains(list []any, wanted string) bool {
	for _, item := range list {
		if str(item) == wanted {
			return true
		}
	}
	return false
}

func checkParameters(values, task map[string]any) (map[string]any, error) {
	declared, _ := object(task["parameters"])
	expanded := map[string]any{}
	for key, value := range values {
		if _, ok := declared[key]; !ok {
			return nil, fail("unknown_parameter", key)
		}
		expanded[key] = value
	}
	for key, raw := range declared {
		rule, _ := object(raw)
		value, exists := expanded[key]
		if !exists {
			if def, ok := rule["default"]; ok {
				value = def
				expanded[key] = def
				exists = true
			} else if rule["required"] == true {
				return nil, fail("missing_parameter", key)
			}
		}
		if !exists {
			continue
		}
		typ := str(rule["type"])
		switch typ {
		case "string":
			if _, ok := value.(string); !ok {
				return nil, fail("semantic_invalid", "wrong parameter type: "+key)
			}
		case "boolean":
			if _, ok := value.(bool); !ok {
				return nil, fail("semantic_invalid", "wrong parameter type: "+key)
			}
		case "integer":
			if _, ok := value.(json.Number); !ok {
				return nil, fail("semantic_invalid", "wrong parameter type: "+key)
			}
		}
		if options, ok := rule["enum"].([]any); ok {
			n, err := normalized(value)
			if err != nil {
				return nil, err
			}
			found := false
			for _, option := range options {
				no, err := normalized(option)
				if err != nil {
					return nil, err
				}
				if reflect.DeepEqual(n, no) {
					found = true
					break
				}
			}
			if !found {
				return nil, fail("semantic_invalid", "value outside enum: "+key)
			}
		}
	}
	n, err := normalized(expanded)
	if err != nil {
		return nil, err
	}
	return n.(map[string]any), nil
}

func same(a, b any) bool                   { return reflect.DeepEqual(a, b) }
func ref(id, digest string) map[string]any { return map[string]any{"id": id, "sha256": digest} }
func needed(ok bool, code, detail string) error {
	if !ok {
		return fail(code, detail)
	}
	return nil
}

func (c *Checker) CheckBundle(bundle map[string]any) (map[string]any, error) {
	for _, kind := range kinds[:len(kinds)-1] {
		if err := c.structure(bundle[kind], kind); err != nil {
			return nil, err
		}
	}
	manifest, _ := object(bundle["definition_manifest"])
	run, _ := object(bundle["run_context"])
	attempt, _ := object(bundle["attempt_context"])
	job, _ := object(bundle["job_ref"])
	artifact, _ := object(bundle["artifact"])
	expectation, _ := object(bundle["expectation"])
	manifestSHA, err := ContentSHA(manifest, "content_sha256", "definition_id")
	if err != nil {
		return nil, err
	}
	if err := needed(str(manifest["content_sha256"]) == manifestSHA && str(manifest["definition_id"]) == "def_"+manifestSHA, "semantic_invalid", "manifest ID/hash"); err != nil {
		return nil, err
	}
	tasks := map[string]map[string]any{}
	for _, raw := range arr(manifest["tasks"]) {
		task, _ := object(raw)
		key := str(task["task_key"])
		if _, ok := tasks[key]; ok {
			return nil, fail("semantic_invalid", "duplicate task key")
		}
		tasks[key] = task
	}
	stages := arr(manifest["stage_definitions"])
	for _, task := range tasks {
		if !contains(stages, str(task["stage_key"])) {
			return nil, fail("semantic_invalid", "task stage missing")
		}
		for _, dep := range arr(task["depends_on"]) {
			if _, ok := tasks[str(dep)]; !ok {
				return nil, fail("semantic_invalid", "task dependency missing")
			}
		}
		declared, _ := object(task["parameters"])
		for key, raw := range declared {
			rule, _ := object(raw)
			if def, ok := rule["default"]; ok {
				_, err := checkParameters(map[string]any{key: def}, map[string]any{"parameters": map[string]any{key: rule}})
				if err != nil {
					return nil, err
				}
			}
			for _, option := range arr(rule["enum"]) {
				if _, err := checkParameters(map[string]any{key: option}, map[string]any{"parameters": map[string]any{key: rule}}); err != nil {
					return nil, err
				}
			}
		}
	}
	if err := needed(same(run["definition_ref"], ref(str(manifest["definition_id"]), manifestSHA)), "parent_mismatch", "definition reference"); err != nil {
		return nil, err
	}
	scope := map[string]bool{}
	for _, raw := range arr(run["scope"]) {
		entry, _ := object(raw)
		key := str(entry["task_key"])
		task, ok := tasks[key]
		if !ok {
			return nil, fail("parent_mismatch", "unknown task in scope")
		}
		params, _ := object(entry["parameters"])
		expanded, err := checkParameters(params, task)
		if err != nil {
			return nil, err
		}
		expandedBytes, err := Canonical(expanded)
		if err != nil {
			return nil, err
		}
		paramsBytes, err := Canonical(params)
		if err != nil {
			return nil, err
		}
		if !bytes.Equal(expandedBytes, paramsBytes) {
			return nil, fail("missing_parameter", "scope defaults must be expanded")
		}
		data, err := Canonical(expanded)
		if err != nil {
			return nil, err
		}
		scopeKey := key + "|" + string(data)
		if scope[scopeKey] {
			return nil, fail("semantic_invalid", "duplicate task instance in scope")
		}
		scope[scopeKey] = true
	}
	runSHA, err := ContentSHA(run, "context_sha256")
	if err != nil {
		return nil, err
	}
	if err := needed(str(run["context_sha256"]) == runSHA, "semantic_invalid", "run context hash"); err != nil {
		return nil, err
	}
	expSHA, err := ContentSHA(expectation, "plan_id")
	if err != nil {
		return nil, err
	}
	if err := needed(str(expectation["plan_id"]) == "exp_"+expSHA, "semantic_invalid", "expectation ID"); err != nil {
		return nil, err
	}
	if run["expectation_ref"] != nil {
		if err := needed(same(run["expectation_ref"], ref(str(expectation["plan_id"]), expSHA)), "parent_mismatch", "expectation reference"); err != nil {
			return nil, err
		}
	}
	for _, field := range []string{"system_id", "purpose", "deployment_id"} {
		if err := needed(same(run[field], expectation[field]), "parent_mismatch", "expectation scope"); err != nil {
			return nil, err
		}
	}
	foundCycle := false
	cycleKeys := map[string]bool{}
	for _, raw := range arr(expectation["cycles"]) {
		item, _ := object(raw)
		if same(item["cycle"], run["cycle"]) && str(item["status"]) == "expected" {
			foundCycle = true
		}
		if str(item["status"]) == "cancelled" && str(item["reason"]) == "" {
			return nil, fail("semantic_invalid", "cancelled cycle needs reason")
		}
		key, err := Canonical(item["cycle"])
		if err != nil {
			return nil, err
		}
		if cycleKeys[string(key)] {
			return nil, fail("semantic_invalid", "duplicate expected cycle")
		}
		cycleKeys[string(key)] = true
	}
	if !foundCycle {
		return nil, fail("parent_mismatch", "run cycle absent from expectation")
	}
	slots := map[string]map[string]any{}
	for _, raw := range arr(expectation["slots"]) {
		item, _ := object(raw)
		id := str(item["slot_id"])
		if _, exists := slots[id]; exists {
			return nil, fail("semantic_invalid", "duplicate expectation slot")
		}
		slots[id] = item
	}
	if err := needed(same(attempt["run_id"], run["run_id"]) && same(attempt["run_context_sha256"], run["context_sha256"]), "parent_mismatch", "attempt parent"); err != nil {
		return nil, err
	}
	task, ok := tasks[str(attempt["task_key"])]
	if !ok {
		return nil, fail("parent_mismatch", "attempt task")
	}
	params, _ := object(attempt["parameters"])
	expanded, err := checkParameters(params, task)
	if err != nil {
		return nil, err
	}
	expandedBytes, err := Canonical(expanded)
	if err != nil {
		return nil, err
	}
	paramsBytes, err := Canonical(params)
	if err != nil {
		return nil, err
	}
	if !bytes.Equal(expandedBytes, paramsBytes) {
		return nil, fail("missing_parameter", "attempt defaults must be expanded")
	}
	data, err := Canonical(expanded)
	if err != nil {
		return nil, err
	}
	if !scope[str(attempt["task_key"])+"|"+string(data)] {
		return nil, fail("parent_mismatch", "attempt outside run scope")
	}
	instanceID, err := TaskInstanceID(str(run["run_id"]), str(attempt["task_key"]), expanded)
	if err != nil {
		return nil, err
	}
	if err := needed(str(attempt["task_instance_id"]) == instanceID, "parent_mismatch", "task instance ID"); err != nil {
		return nil, err
	}
	if attempt["retry_of"] != nil && same(attempt["retry_of"], attempt["attempt_id"]) {
		return nil, fail("semantic_invalid", "self retry")
	}
	attemptSHA, err := ContentSHA(attempt, "context_sha256")
	if err != nil {
		return nil, err
	}
	if err := needed(str(attempt["context_sha256"]) == attemptSHA, "semantic_invalid", "attempt context hash"); err != nil {
		return nil, err
	}
	if !same(job["run_id"], run["run_id"]) || !same(job["attempt_id"], attempt["attempt_id"]) || !same(job["platform_id"], run["platform_id"]) {
		return nil, fail("parent_mismatch", "job parent/platform")
	}
	expectedJobID := "job_" + LengthDigest(str(job["platform_id"]), str(job["scheduler"]), str(job["cluster_id"]), str(job["scheduler_job_id"]), str(job["submission_instance"]))
	if str(job["job_ref_id"]) != expectedJobID {
		return nil, fail("semantic_invalid", "job composite identity")
	}
	if !same(artifact["cycle"], run["cycle"]) {
		return nil, fail("parent_mismatch", "artifact cycle")
	}
	if artifact["producer_run_id"] != nil && !same(artifact["producer_run_id"], run["run_id"]) {
		return nil, fail("parent_mismatch", "artifact run")
	}
	if artifact["producer_attempt_id"] != nil && (!same(artifact["producer_attempt_id"], attempt["attempt_id"]) || artifact["producer_run_id"] == nil) {
		return nil, fail("parent_mismatch", "artifact attempt")
	}
	versionEvidence, _ := object(artifact["version_evidence"])
	if str(versionEvidence["kind"]) == "content_sha256" && !regexp.MustCompile(`^[0-9a-f]{64}$`).MatchString(str(versionEvidence["value"])) {
		return nil, fail("semantic_invalid", "invalid artifact content hash")
	}
	artifactSHA, err := ContentSHA(artifact, "artifact_version_id")
	if err != nil {
		return nil, err
	}
	if err := needed(str(artifact["artifact_version_id"]) == "av_"+artifactSHA, "semantic_invalid", "artifact version ID"); err != nil {
		return nil, err
	}
	seen := map[string]string{}
	opaque := []string{}
	opaqueEvents := []any{}
	for _, raw := range arr(bundle["events"]) {
		if err := c.structure(raw, "event"); err != nil {
			return nil, err
		}
		event, _ := object(raw)
		id := str(event["event_id"])
		encoded, err := Canonical(event)
		if err != nil {
			return nil, err
		}
		if prior, exists := seen[id]; exists {
			if prior != string(encoded) {
				return nil, fail("conflicting_event_id", id)
			}
			continue
		}
		seen[id] = string(encoded)
		if !strings.HasPrefix(id, "evt_") && !uuidV4.MatchString(id) {
			return nil, fail("semantic_invalid", "ordinary event ID must be canonical UUIDv4")
		}
		assoc, _ := object(event["association"])
		if !same(assoc["run_id"], run["run_id"]) {
			return nil, fail("parent_mismatch", "event run")
		}
		if ti, ok := assoc["task_instance_id"]; ok && !same(ti, attempt["task_instance_id"]) {
			return nil, fail("parent_mismatch", "event task instance")
		}
		if at, ok := assoc["attempt_id"]; ok {
			if !same(at, attempt["attempt_id"]) {
				return nil, fail("parent_mismatch", "event attempt")
			}
			if _, exists := assoc["task_instance_id"]; !exists {
				return nil, fail("parent_mismatch", "event attempt without instance")
			}
		}
		if jobID, ok := assoc["job_ref_id"]; ok && !same(jobID, job["job_ref_id"]) {
			return nil, fail("parent_mismatch", "event job reference")
		}
		quality, _ := object(event["time_quality"])
		if event["occurred_at"] == nil || str(quality["basis"]) == "unknown" {
			if str(quality["reason"]) == "" {
				return nil, fail("semantic_invalid", "unknown time needs reason")
			}
		}
		if str(event["event_type"]) == "task.success" {
			return nil, fail("semantic_invalid", "task.success is reserved and forbidden")
		}
		entryRaw, known := c.Catalog[str(event["event_type"])]
		if !known {
			opaque = append(opaque, id)
			opaqueEvents = append(opaqueEvents, event)
			continue
		}
		entry, _ := object(entryRaw)
		producer, _ := object(event["producer"])
		source, _ := object(event["source"])
		if !contains(arr(entry["roles"]), str(producer["role"])) || !contains(arr(entry["source_kinds"]), str(source["kind"])) {
			return nil, fail("unauthorized_event", str(event["event_type"]))
		}
		level := str(entry["association"])
		if level == "instance" || level == "attempt" {
			if _, ok := assoc["task_instance_id"]; !ok {
				return nil, fail("parent_mismatch", "event lacks task instance")
			}
		}
		if level == "attempt" {
			if _, ok := assoc["attempt_id"]; !ok {
				return nil, fail("parent_mismatch", "event lacks attempt")
			}
		}
		if entry["evidence_required"] == true && len(arr(event["evidence_refs"])) == 0 {
			return nil, fail("semantic_invalid", "evidence required")
		}
		for _, rawEvidence := range arr(event["evidence_refs"]) {
			evidence, _ := object(rawEvidence)
			start, hasStart := evidence["byte_start"].(json.Number)
			end, hasEnd := evidence["byte_end"].(json.Number)
			if hasStart && hasEnd {
				startNum, _ := start.Int64()
				endNum, _ := end.Int64()
				if endNum < startNum {
					return nil, fail("semantic_invalid", "evidence byte range")
				}
			}
		}
		if err := c.PayloadSchemas[str(event["event_type"])].Validate(event["payload"]); err != nil {
			return nil, fail("schema_invalid", str(event["event_type"])+": "+err.Error())
		}
		p, _ := object(event["payload"])
		if err := checkTimestamps(p); err != nil {
			return nil, err
		}
		switch str(event["event_type"]) {
		case "attempt.exited":
			_, code := p["exit_code"]
			_, signal := p["signal"]
			if (str(p["termination"]) == "exited" && (!code || signal)) || (str(p["termination"]) == "signaled" && (!signal || code)) {
				return nil, fail("semantic_invalid", "termination/code/signal mismatch")
			}
		case "run.registered":
			if !same(p["run_context_sha256"], run["context_sha256"]) || !same(p["request_sha256"], run["request_sha256"]) {
				return nil, fail("parent_mismatch", "run registration payload")
			}
		case "attempt.registered":
			if !same(p["attempt_context_sha256"], attempt["context_sha256"]) || !same(p["execution_token_sha256"], attempt["execution_token_sha256"]) || !same(p["retry_of"], attempt["retry_of"]) {
				return nil, fail("parent_mismatch", "attempt registration payload")
			}
		case "run.finish_declared":
			ids := arr(p["covered_attempt_ids"])
			sorted := make([]string, len(ids))
			for i, value := range ids {
				sorted[i] = str(value)
			}
			sort.Strings(sorted)
			for i, value := range ids {
				if str(value) != sorted[i] {
					return nil, fail("semantic_invalid", "covered attempt set order")
				}
			}
			digest, err := SHA(ids)
			if err != nil {
				return nil, err
			}
			if str(p["covered_attempts_sha256"]) != digest {
				return nil, fail("semantic_invalid", "covered attempt set hash")
			}
		case "attempt.selection_declared":
			if !contains(arr(p["candidate_attempt_ids"]), str(p["selected_attempt_id"])) {
				return nil, fail("semantic_invalid", "selected attempt outside candidates")
			}
		}
		if str(event["event_type"]) == "submission.accepted" || str(event["event_type"]) == "job.state_observed" {
			if !same(p["job_ref_id"], job["job_ref_id"]) || !same(assoc["job_ref_id"], job["job_ref_id"]) {
				return nil, fail("parent_mismatch", "event job reference")
			}
		}
		if str(event["event_type"]) == "submission.accepted" && !same(p["submission_instance"], job["submission_instance"]) {
			return nil, fail("parent_mismatch", "submission generation")
		}
		if str(event["event_type"]) == "artifact.validation_completed" && !same(p["artifact_version_ref"], artifact["artifact_version_id"]) {
			return nil, fail("parent_mismatch", "validation artifact version")
		}
		if str(event["event_type"]) == "delivery.confirmed" {
			slot, exists := slots[str(p["expectation_slot"])]
			if !exists || !same(p["artifact_version_ref"], artifact["artifact_version_id"]) ||
				!same(slot["artifact_id"], artifact["artifact_id"]) || !same(p["recipient"], slot["recipient"]) ||
				!same(p["rule_version"], slot["rule_version"]) {
				return nil, fail("parent_mismatch", "delivery expectation/artifact binding")
			}
		}
		if contains([]any{"attempt.started", "attempt.exited", "stage.started", "stage.ended"}, str(event["event_type"])) && event["occurred_at"] == nil {
			return nil, fail("semantic_invalid", "direct process/stage boundary needs occurred_at")
		}
	}
	bundleSHA, err := SHA(bundle)
	if err != nil {
		return nil, err
	}
	return map[string]any{"known": len(seen) - len(opaque), "opaque": opaque, "opaque_events": opaqueEvents, "event_count": len(seen), "bundle_canonical_sha256": bundleSHA}, nil
}
