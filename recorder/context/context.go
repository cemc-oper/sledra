// Package context implements the immutable Run/Attempt lifecycle above the
// file Registry. It never discovers a Run from a cycle or submits work.
package context

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"os"
	"path/filepath"
	"regexp"
	"sort"

	"sledra.local/recorder/event"
	"sledra.local/recorder/internal/registry"
	"sledra.local/recorder/internal/speccontract"
)

type Error struct{ Code string }

func (e *Error) Error() string           { return e.Code }
func fail(code string) error             { return &Error{Code: code} }
func isCode(err error, code string) bool { var e *Error; return errors.As(err, &e) && e.Code == code }
func str(v any) string                   { s, _ := v.(string); return s }
func obj(v any) map[string]any           { x, _ := v.(map[string]any); return x }
func list(v any) []any                   { x, _ := v.([]any); return x }
func shaString(s string) string          { h := sha256.Sum256([]byte(s)); return hex.EncodeToString(h[:]) }
func optional(p *string) any {
	if p == nil {
		return nil
	}
	return *p
}

type Service struct {
	root        string
	registry    *registry.Registry
	checker     *speccontract.Checker
	finishCodec *event.Codec
}

func New(root, specDir string) (*Service, error) {
	if !filepath.IsAbs(root) {
		return nil, fail("path_invalid")
	}
	root = filepath.Clean(root)
	info, err := os.Lstat(root)
	if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
		return nil, fail("path_unavailable")
	}
	r, err := registry.New(root, specDir)
	if err != nil {
		return nil, fail("registry_unavailable")
	}
	c, err := speccontract.NewChecker(specDir)
	if err != nil {
		return nil, fail("contract_unavailable")
	}
	codec, err := event.New("lifecycle_owner")
	if err != nil {
		return nil, fail("contract_unavailable")
	}
	return &Service{root: root, registry: r, checker: c, finishCodec: codec}, nil
}

type ScopeInput struct {
	TaskKey    string         `json:"task_key"`
	Parameters map[string]any `json:"parameters"`
}
type RunRequest struct {
	RegistryID   string         `json:"registry_id"`
	LaunchToken  string         `json:"launch_token"`
	SystemID     string         `json:"system_id"`
	Purpose      string         `json:"purpose"`
	DeploymentID string         `json:"deployment_id"`
	PlatformID   string         `json:"platform_id"`
	Cycle        map[string]any `json:"cycle"`
	Definition   map[string]any `json:"-"`
	Expectation  map[string]any `json:"-"`
	ConfigRefs   []any          `json:"config_refs"`
	Scope        []ScopeInput   `json:"scope"`
	RerunOf      *string        `json:"rerun_of"`
}
type AttemptRequest struct {
	Run            map[string]any `json:"-"`
	TaskKey        string         `json:"task_key"`
	Parameters     map[string]any `json:"parameters"`
	ExecutionToken string         `json:"execution_token"`
	RetryOf        *string        `json:"retry_of"`
	ProcessRole    string         `json:"process_role"`
}
type Result struct {
	ID         string
	Context    map[string]any
	RecordPath string
	Created    bool
}

func canonicalMap(v map[string]any) (map[string]any, error) {
	if v == nil {
		return nil, fail("schema_invalid")
	}
	b, err := speccontract.Canonical(v)
	if err != nil {
		return nil, fail("schema_invalid")
	}
	x, err := speccontract.Decode(b)
	if err != nil {
		return nil, fail("schema_invalid")
	}
	return x.(map[string]any), nil
}
func equal(a, b any) bool {
	x, e1 := speccontract.Canonical(a)
	y, e2 := speccontract.Canonical(b)
	return e1 == nil && e2 == nil && bytes.Equal(x, y)
}
func registryCode(err error) error {
	var e *registry.Error
	if errors.As(err, &e) {
		return fail(e.Code)
	}
	return fail("registry_unavailable")
}
func authorityCode(err error) error {
	var e *registry.Error
	if errors.As(err, &e) && e.Code == "corrupt_record" {
		return fail("context_invalid")
	}
	return fail("context_missing")
}

func (s *Service) validateManifest(doc map[string]any) (map[string]map[string]any, error) {
	if err := s.checker.Schemas["definition_manifest"].Validate(doc); err != nil {
		return nil, fail("schema_invalid")
	}
	digest, err := speccontract.ContentSHA(doc, "content_sha256", "definition_id")
	if err != nil || str(doc["content_sha256"]) != digest || str(doc["definition_id"]) != "def_"+digest {
		return nil, fail("semantic_invalid")
	}
	tasks := map[string]map[string]any{}
	stages := map[string]bool{}
	for _, v := range list(doc["stage_definitions"]) {
		stages[str(v)] = true
	}
	for _, raw := range list(doc["tasks"]) {
		t := obj(raw)
		key := str(t["task_key"])
		if tasks[key] != nil || !stages[str(t["stage_key"])] {
			return nil, fail("semantic_invalid")
		}
		tasks[key] = t
	}
	for _, t := range tasks {
		for _, dep := range list(t["depends_on"]) {
			if tasks[str(dep)] == nil {
				return nil, fail("semantic_invalid")
			}
		}
		for key, raw := range obj(t["parameters"]) {
			rule := obj(raw)
			if def, ok := rule["default"]; ok {
				if _, err := speccontract.ExpandParameters(map[string]any{key: def}, map[string]any{"parameters": map[string]any{key: rule}}); err != nil {
					return nil, fail("semantic_invalid")
				}
			}
			for _, option := range list(rule["enum"]) {
				if _, err := speccontract.ExpandParameters(map[string]any{key: option}, map[string]any{"parameters": map[string]any{key: rule}}); err != nil {
					return nil, fail("semantic_invalid")
				}
			}
		}
	}
	return tasks, nil
}

func (s *Service) validateExpectation(doc map[string]any, system, purpose, deployment string, cycle map[string]any) error {
	if err := s.checker.Schemas["expectation"].Validate(doc); err != nil {
		return fail("schema_invalid")
	}
	digest, err := speccontract.ContentSHA(doc, "plan_id")
	if err != nil || str(doc["plan_id"]) != "exp_"+digest {
		return fail("semantic_invalid")
	}
	if str(doc["system_id"]) != system || str(doc["purpose"]) != purpose || str(doc["deployment_id"]) != deployment {
		return fail("parent_mismatch")
	}
	seen := map[string]bool{}
	found := false
	for _, raw := range list(doc["cycles"]) {
		item := obj(raw)
		key, _ := speccontract.Canonical(item["cycle"])
		if seen[string(key)] {
			return fail("semantic_invalid")
		}
		seen[string(key)] = true
		if equal(item["cycle"], cycle) && item["status"] == "expected" {
			found = true
		}
		if item["status"] == "cancelled" && str(item["reason"]) == "" {
			return fail("semantic_invalid")
		}
	}
	if !found {
		return fail("parent_mismatch")
	}
	slots := map[string]bool{}
	for _, raw := range list(doc["slots"]) {
		id := str(obj(raw)["slot_id"])
		if slots[id] {
			return fail("semantic_invalid")
		}
		slots[id] = true
	}
	return nil
}

func (s *Service) document(kind string, doc map[string]any) error {
	id := str(doc["definition_id"])
	if kind == "expectation" {
		id = str(doc["plan_id"])
	}
	if !regexp.MustCompile(`^(def_|exp_)[0-9a-f]{64}$`).MatchString(id) {
		return fail("schema_invalid")
	}
	b, err := speccontract.Canonical(doc)
	if err != nil {
		return fail("schema_invalid")
	}
	return publishEqual(s.root, filepath.Join(s.root, "documents", kind, id+".json"), b)
}
func (s *Service) loadDocument(kind, id string) (map[string]any, error) {
	if !regexp.MustCompile(`^(def_|exp_)[0-9a-f]{64}$`).MatchString(id) {
		return nil, fail("parent_mismatch")
	}
	path := filepath.Join(s.root, "documents", kind, id+".json")
	if err := realPath(s.root, path, false); err != nil {
		return nil, err
	}
	b, err := readBounded(path)
	if err != nil {
		return nil, err
	}
	v, err := speccontract.Decode(b)
	if err != nil {
		return nil, fail("context_invalid")
	}
	m, ok := v.(map[string]any)
	if !ok {
		return nil, fail("context_invalid")
	}
	return m, nil
}

func (s *Service) CreateRun(req RunRequest) (*Result, error) {
	if req.RegistryID == "" || req.LaunchToken == "" || req.SystemID == "" || req.Purpose == "" || req.DeploymentID == "" || req.PlatformID == "" || len(req.Scope) == 0 {
		return nil, fail("schema_invalid")
	}
	manifest, err := canonicalMap(req.Definition)
	if err != nil {
		return nil, err
	}
	tasks, err := s.validateManifest(manifest)
	if err != nil {
		return nil, err
	}
	cycle, err := canonicalMap(req.Cycle)
	if err != nil {
		return nil, err
	}
	var expectation map[string]any
	var expectationRef any
	if req.Expectation != nil {
		expectation, err = canonicalMap(req.Expectation)
		if err != nil {
			return nil, err
		}
		if err := s.validateExpectation(expectation, req.SystemID, req.Purpose, req.DeploymentID, cycle); err != nil {
			return nil, err
		}
		sha := str(expectation["plan_id"])[4:]
		expectationRef = map[string]any{"id": expectation["plan_id"], "sha256": sha}
	}
	if req.RerunOf != nil {
		prior, err := s.registry.Read(req.RegistryID, "run", *req.RerunOf)
		if err != nil {
			return nil, fail("parent_mismatch")
		}
		p := obj(prior.Record["context"])
		if str(p["system_id"]) != req.SystemID || str(p["deployment_id"]) != req.DeploymentID || !equal(p["cycle"], cycle) {
			return nil, fail("parent_mismatch")
		}
	}
	runID := speccontract.RegistryID(req.RegistryID, "run", req.DeploymentID, req.LaunchToken)
	entries := make([]any, 0, len(req.Scope))
	seen := map[string]bool{}
	for _, entry := range req.Scope {
		task := tasks[entry.TaskKey]
		if task == nil {
			return nil, fail("parent_mismatch")
		}
		params, err := speccontract.ExpandParameters(entry.Parameters, task)
		if err != nil {
			return nil, fail("semantic_invalid")
		}
		key, _ := speccontract.Canonical(map[string]any{"task_key": entry.TaskKey, "parameters": params})
		if seen[string(key)] {
			return nil, fail("semantic_invalid")
		}
		seen[string(key)] = true
		entries = append(entries, map[string]any{"task_key": entry.TaskKey, "parameters": params})
	}
	sort.Slice(entries, func(i, j int) bool {
		a, _ := speccontract.Canonical(entries[i])
		b, _ := speccontract.Canonical(entries[j])
		return bytes.Compare(a, b) < 0
	})
	configRefs := req.ConfigRefs
	if configRefs == nil {
		configRefs = []any{}
	}
	request := map[string]any{"registry_id": req.RegistryID, "launch_token": req.LaunchToken, "system_id": req.SystemID, "purpose": req.Purpose, "deployment_id": req.DeploymentID, "platform_id": req.PlatformID, "cycle": cycle, "definition_ref": map[string]any{"id": manifest["definition_id"], "sha256": manifest["content_sha256"]}, "expectation_ref": expectationRef, "config_refs": configRefs, "scope": entries, "rerun_of": optional(req.RerunOf)}
	requestSHA, err := speccontract.SHA(request)
	if err != nil {
		return nil, fail("schema_invalid")
	}
	ctx := map[string]any{"schema_version": "1.0.0", "kind": "run_context", "registry_id": req.RegistryID, "run_id": runID, "request_sha256": requestSHA, "launch_token_sha256": shaString(req.LaunchToken), "system_id": req.SystemID, "purpose": req.Purpose, "deployment_id": req.DeploymentID, "platform_id": req.PlatformID, "cycle": cycle, "definition_ref": request["definition_ref"], "expectation_ref": expectationRef, "config_refs": configRefs, "scope": entries, "rerun_of": optional(req.RerunOf)}
	ctx["context_sha256"], err = speccontract.ContentSHA(ctx, "context_sha256")
	if err != nil {
		return nil, fail("schema_invalid")
	}
	if err := s.checker.Schemas["run_context"].Validate(ctx); err != nil {
		return nil, fail("schema_invalid")
	}
	if err := s.document("definition", manifest); err != nil {
		return nil, err
	}
	if expectation != nil {
		if err := s.document("expectation", expectation); err != nil {
			return nil, err
		}
	}
	registered, err := s.registry.Register(registry.Request{Kind: "run", RegistryID: req.RegistryID, ParentID: req.DeploymentID, Token: req.LaunchToken, Context: ctx})
	if err != nil {
		return nil, registryCode(err)
	}
	return &Result{ID: registered.ID, Context: obj(registered.Record["context"]), RecordPath: registered.Path, Created: registered.Created}, nil
}

func (s *Service) CreateAttempt(req AttemptRequest) (*Result, error) {
	if req.TaskKey == "" || req.ExecutionToken == "" || req.ProcessRole == "" {
		return nil, fail("schema_invalid")
	}
	run, err := s.ValidateRun(req.Run)
	if err != nil {
		return nil, err
	}
	manifest, err := s.loadDocument("definition", str(obj(run["definition_ref"])["id"]))
	if err != nil {
		return nil, err
	}
	tasks, err := s.validateManifest(manifest)
	if err != nil {
		return nil, err
	}
	task := tasks[req.TaskKey]
	if task == nil {
		return nil, fail("parent_mismatch")
	}
	params, err := speccontract.ExpandParameters(req.Parameters, task)
	if err != nil {
		return nil, fail("semantic_invalid")
	}
	found := false
	for _, raw := range list(run["scope"]) {
		entry := obj(raw)
		if str(entry["task_key"]) == req.TaskKey && equal(entry["parameters"], params) {
			found = true
			break
		}
	}
	if !found {
		return nil, fail("parent_mismatch")
	}
	instanceID, err := speccontract.TaskInstanceID(str(run["run_id"]), req.TaskKey, params)
	if err != nil {
		return nil, fail("semantic_invalid")
	}
	id := speccontract.RegistryID(str(run["registry_id"]), "attempt", instanceID, req.ExecutionToken)
	if req.RetryOf != nil {
		if *req.RetryOf == id {
			return nil, fail("semantic_invalid")
		}
		prior, err := s.registry.Read(str(run["registry_id"]), "attempt", *req.RetryOf)
		if err != nil {
			return nil, fail("parent_mismatch")
		}
		p := obj(prior.Record["context"])
		if str(p["run_id"]) != str(run["run_id"]) || str(p["run_context_sha256"]) != str(run["context_sha256"]) || str(p["task_instance_id"]) != instanceID || str(p["task_key"]) != req.TaskKey || !equal(p["parameters"], params) {
			return nil, fail("parent_mismatch")
		}
	}
	ctx := map[string]any{"schema_version": "1.0.0", "kind": "attempt_context", "run_id": run["run_id"], "run_context_sha256": run["context_sha256"], "task_key": req.TaskKey, "parameters": params, "task_instance_id": instanceID, "attempt_id": id, "execution_token_sha256": shaString(req.ExecutionToken), "retry_of": optional(req.RetryOf), "process_role": req.ProcessRole, "extensions": map[string]any{"sledra.parent": map[string]any{"registry_id": run["registry_id"]}}}
	ctx["context_sha256"], err = speccontract.ContentSHA(ctx, "context_sha256")
	if err != nil {
		return nil, fail("schema_invalid")
	}
	if err := s.checker.Schemas["attempt_context"].Validate(ctx); err != nil {
		return nil, fail("schema_invalid")
	}
	registered, err := s.registry.Register(registry.Request{Kind: "attempt", RegistryID: str(run["registry_id"]), ParentID: instanceID, Token: req.ExecutionToken, Context: ctx})
	if err != nil {
		return nil, registryCode(err)
	}
	return &Result{ID: registered.ID, Context: obj(registered.Record["context"]), RecordPath: registered.Path, Created: registered.Created}, nil
}

func (s *Service) ValidateRun(input map[string]any) (map[string]any, error) {
	run, err := canonicalMap(input)
	if err != nil {
		return nil, err
	}
	if err := s.checker.Schemas["run_context"].Validate(run); err != nil {
		return nil, fail("schema_invalid")
	}
	digest, err := speccontract.ContentSHA(run, "context_sha256")
	if err != nil || str(run["context_sha256"]) != digest {
		return nil, fail("context_invalid")
	}
	record, err := s.registry.Read(str(run["registry_id"]), "run", str(run["run_id"]))
	if err != nil {
		return nil, authorityCode(err)
	}
	if !equal(run, record.Record["context"]) {
		return nil, fail("context_invalid")
	}
	if run["rerun_of"] != nil {
		priorID := str(run["rerun_of"])
		if priorID == str(run["run_id"]) {
			return nil, fail("semantic_invalid")
		}
		prior, err := s.registry.Read(str(run["registry_id"]), "run", priorID)
		if err != nil {
			return nil, fail("parent_mismatch")
		}
		p := obj(prior.Record["context"])
		if str(p["system_id"]) != str(run["system_id"]) || str(p["deployment_id"]) != str(run["deployment_id"]) || !equal(p["cycle"], run["cycle"]) {
			return nil, fail("parent_mismatch")
		}
	}
	manifest, err := s.loadDocument("definition", str(obj(run["definition_ref"])["id"]))
	if err != nil {
		return nil, err
	}
	tasks, err := s.validateManifest(manifest)
	if err != nil {
		return nil, err
	}
	if !equal(run["definition_ref"], map[string]any{"id": manifest["definition_id"], "sha256": manifest["content_sha256"]}) {
		return nil, fail("parent_mismatch")
	}
	if run["expectation_ref"] != nil {
		expectation, err := s.loadDocument("expectation", str(obj(run["expectation_ref"])["id"]))
		if err != nil {
			return nil, err
		}
		if err := s.validateExpectation(expectation, str(run["system_id"]), str(run["purpose"]), str(run["deployment_id"]), obj(run["cycle"])); err != nil {
			return nil, err
		}
		if !equal(run["expectation_ref"], map[string]any{"id": expectation["plan_id"], "sha256": str(expectation["plan_id"])[4:]}) {
			return nil, fail("parent_mismatch")
		}
	}
	seen := map[string]bool{}
	for _, raw := range list(run["scope"]) {
		entry := obj(raw)
		task := tasks[str(entry["task_key"])]
		if task == nil {
			return nil, fail("parent_mismatch")
		}
		expanded, err := speccontract.ExpandParameters(obj(entry["parameters"]), task)
		if err != nil || !equal(expanded, entry["parameters"]) {
			return nil, fail("parent_mismatch")
		}
		key, _ := speccontract.Canonical(entry)
		if seen[string(key)] {
			return nil, fail("semantic_invalid")
		}
		seen[string(key)] = true
	}
	return run, nil
}

func (s *Service) ValidateAttempt(input map[string]any) (map[string]any, error) {
	attempt, err := canonicalMap(input)
	if err != nil {
		return nil, err
	}
	if err := s.checker.Schemas["attempt_context"].Validate(attempt); err != nil {
		return nil, fail("schema_invalid")
	}
	digest, err := speccontract.ContentSHA(attempt, "context_sha256")
	if err != nil || str(attempt["context_sha256"]) != digest {
		return nil, fail("context_invalid")
	}
	parent := obj(obj(attempt["extensions"])["sledra.parent"])
	registryID := str(parent["registry_id"])
	if registryID == "" {
		return nil, fail("context_invalid")
	}
	stored, err := s.registry.Read(registryID, "attempt", str(attempt["attempt_id"]))
	if err != nil {
		return nil, authorityCode(err)
	}
	if !equal(attempt, stored.Record["context"]) {
		return nil, fail("context_invalid")
	}
	runRecord, err := s.registry.Read(registryID, "run", str(attempt["run_id"]))
	if err != nil {
		return nil, fail("parent_mismatch")
	}
	run, err := s.ValidateRun(obj(runRecord.Record["context"]))
	if err != nil {
		return nil, err
	}
	if str(attempt["run_context_sha256"]) != str(run["context_sha256"]) {
		return nil, fail("parent_mismatch")
	}
	manifest, err := s.loadDocument("definition", str(obj(run["definition_ref"])["id"]))
	if err != nil {
		return nil, err
	}
	tasks, err := s.validateManifest(manifest)
	if err != nil {
		return nil, err
	}
	task := tasks[str(attempt["task_key"])]
	if task == nil {
		return nil, fail("parent_mismatch")
	}
	params, err := speccontract.ExpandParameters(obj(attempt["parameters"]), task)
	if err != nil || !equal(params, attempt["parameters"]) {
		return nil, fail("parent_mismatch")
	}
	instanceID, err := speccontract.TaskInstanceID(str(run["run_id"]), str(attempt["task_key"]), params)
	if err != nil || instanceID != str(attempt["task_instance_id"]) {
		return nil, fail("parent_mismatch")
	}
	found := false
	for _, raw := range list(run["scope"]) {
		entry := obj(raw)
		if str(entry["task_key"]) == str(attempt["task_key"]) && equal(entry["parameters"], params) {
			found = true
			break
		}
	}
	if !found {
		return nil, fail("parent_mismatch")
	}
	if attempt["retry_of"] != nil {
		priorID := str(attempt["retry_of"])
		if priorID == str(attempt["attempt_id"]) {
			return nil, fail("semantic_invalid")
		}
		prior, err := s.registry.Read(registryID, "attempt", priorID)
		if err != nil {
			return nil, fail("parent_mismatch")
		}
		p := obj(prior.Record["context"])
		if str(p["run_id"]) != str(run["run_id"]) || str(p["task_instance_id"]) != instanceID {
			return nil, fail("parent_mismatch")
		}
		if str(p["run_context_sha256"]) != str(run["context_sha256"]) || str(p["task_key"]) != str(attempt["task_key"]) || !equal(p["parameters"], params) {
			return nil, fail("parent_mismatch")
		}
	}
	return attempt, nil
}

func (s *Service) PublishContext(path string, doc map[string]any) error {
	if str(doc["kind"]) == "run_context" {
		if _, err := s.ValidateRun(doc); err != nil {
			return err
		}
	} else if str(doc["kind"]) == "attempt_context" {
		if _, err := s.ValidateAttempt(doc); err != nil {
			return err
		}
	} else {
		return fail("schema_invalid")
	}
	b, err := speccontract.Canonical(doc)
	if err != nil {
		return fail("schema_invalid")
	}
	return publishEqual(s.root, path, b)
}

func (s *Service) LoadContext(path string) (map[string]any, error) {
	if err := realPath(s.root, path, false); err != nil {
		return nil, err
	}
	b, err := readBounded(path)
	if err != nil {
		return nil, err
	}
	v, err := speccontract.Decode(b)
	if err != nil {
		return nil, fail("context_invalid")
	}
	doc, ok := v.(map[string]any)
	if !ok {
		return nil, fail("context_invalid")
	}
	if str(doc["kind"]) == "run_context" {
		return s.ValidateRun(doc)
	}
	if str(doc["kind"]) == "attempt_context" {
		return s.ValidateAttempt(doc)
	}
	return nil, fail("schema_invalid")
}
