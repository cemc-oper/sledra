// Package registry publishes immutable Run and Attempt registration records.
// Context construction and lifecycle validation are implemented in context.
package registry

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"syscall"
	"time"
	"unicode/utf8"

	"sledra.local/recorder/internal/speccontract"
)

const maxRecordBytes = 4 << 20

type Error struct {
	Code   string
	Detail string
	Cause  error
}

func (e *Error) Error() string { return e.Code + ": " + e.Detail }
func (e *Error) Unwrap() error { return e.Cause }

func problem(code, detail string, cause error) error {
	return &Error{Code: code, Detail: detail, Cause: cause}
}

// Request contains a completed public context and the caller-owned token.
// The raw token is used to compute the ID and request hash, but is not stored.
type Request struct {
	Kind       string // run or attempt
	RegistryID string
	ParentID   string // deployment_id for Run, task_instance_id for Attempt
	Token      string // launch_token for Run, execution_token for Attempt
	Context    map[string]any
}

type Result struct {
	ID      string
	Path    string
	Created bool
	Record  map[string]any // the winning immutable record, including its outbox event
}

type Registry struct {
	root    string
	checker *speccontract.Checker
	hook    func(string) error // test-only fault injection; never configured by callers
}

// New requires an existing, real directory on a platform already qualified
// for same-filesystem hardlink and directory fsync. It loads schemas offline.
func New(root, specDir string) (*Registry, error) {
	if !filepath.IsAbs(root) {
		return nil, problem("registry_unavailable", "registry root must be absolute", nil)
	}
	info, err := os.Lstat(root)
	if err != nil || !info.IsDir() {
		return nil, problem("registry_unavailable", "registry root is unavailable or not a real directory", err)
	}
	checker, err := speccontract.NewChecker(specDir)
	if err != nil {
		return nil, problem("registry_unavailable", "offline contract resources unavailable", err)
	}
	return &Registry{root: root, checker: checker}, nil
}

func shaBytes(data []byte) string {
	sum := sha256.Sum256(data)
	return hex.EncodeToString(sum[:])
}

func asString(value any) string { s, _ := value.(string); return s }

func canonicalMap(value map[string]any) (map[string]any, error) {
	data, err := speccontract.Canonical(value)
	if err != nil {
		return nil, err
	}
	decoded, err := speccontract.Decode(data)
	if err != nil {
		return nil, err
	}
	return decoded.(map[string]any), nil
}

func (r *Registry) checkContext(kind, registryID, parentID, id, tokenSHA string, context map[string]any) error {
	schemaKind := kind + "_context"
	if err := r.checker.Schemas[schemaKind].Validate(context); err != nil {
		return problem("schema_invalid", "context does not match the public schema", err)
	}
	digest, err := speccontract.ContentSHA(context, "context_sha256")
	if err != nil {
		return problem("schema_invalid", "context cannot be canonicalized", err)
	}
	if asString(context["context_sha256"]) != digest {
		return problem("semantic_invalid", "context digest mismatch", nil)
	}
	if kind == "run" {
		if asString(context["registry_id"]) != registryID || asString(context["deployment_id"]) != parentID ||
			asString(context["run_id"]) != id || asString(context["launch_token_sha256"]) != tokenSHA {
			return problem("semantic_invalid", "Run identity or token digest mismatch", nil)
		}
	} else if asString(context["task_instance_id"]) != parentID || asString(context["attempt_id"]) != id ||
		asString(context["execution_token_sha256"]) != tokenSHA {
		return problem("semantic_invalid", "Attempt identity or token digest mismatch", nil)
	}
	return nil
}

func (r *Registry) prepare(req Request) (map[string]any, string, string, error) {
	if req.Kind != "run" && req.Kind != "attempt" {
		return nil, "", "", problem("schema_invalid", "object kind must be run or attempt", nil)
	}
	if req.RegistryID == "" || req.ParentID == "" || req.Token == "" ||
		!utf8.ValidString(req.RegistryID) || !utf8.ValidString(req.ParentID) || !utf8.ValidString(req.Token) || req.Context == nil {
		return nil, "", "", problem("schema_invalid", "registry, parent, token and context are required", nil)
	}
	id := speccontract.RegistryID(req.RegistryID, req.Kind, req.ParentID, req.Token)
	context, err := canonicalMap(req.Context)
	if err != nil {
		return nil, "", "", problem("schema_invalid", "invalid context JSON", err)
	}
	if err := r.checkContext(req.Kind, req.RegistryID, req.ParentID, id, shaBytes([]byte(req.Token)), context); err != nil {
		return nil, "", "", err
	}
	requestHash, err := speccontract.SHA(map[string]any{
		"registry_id": req.RegistryID, "object_kind": req.Kind, "parent_id": req.ParentID,
		"token": req.Token, "context": context,
	})
	if err != nil {
		return nil, "", "", problem("schema_invalid", "request cannot be canonicalized", err)
	}
	return context, id, requestHash, nil
}

func registrationEvent(kind, registryID, id, at string, context map[string]any) map[string]any {
	association := map[string]any{"run_id": context["run_id"]}
	payload := map[string]any{}
	eventType := kind + ".registered"
	if kind == "run" {
		payload["run_context_sha256"] = context["context_sha256"]
		payload["request_sha256"] = context["request_sha256"]
	} else {
		association["task_instance_id"] = context["task_instance_id"]
		association["attempt_id"] = context["attempt_id"]
		payload["attempt_context_sha256"] = context["context_sha256"]
		payload["execution_token_sha256"] = context["execution_token_sha256"]
		payload["retry_of"] = context["retry_of"]
	}
	return map[string]any{
		"schema_version": "1.0.0", "kind": "event",
		"event_id":   "evt_" + speccontract.LengthDigest("registry-outbox", id, eventType),
		"event_type": eventType, "association": association, "payload": payload,
		"producer":    map[string]any{"name": "sledra-recorder", "version": "0.1.0", "role": "registry"},
		"source":      map[string]any{"kind": "direct", "source_id": registryID, "session_id": id},
		"occurred_at": at, "recorded_at": at, "time_quality": map[string]any{"basis": "producer_clock"},
	}
}

func (r *Registry) buildRecord(req Request, context map[string]any, id, requestHash string) (map[string]any, []byte, error) {
	at := time.Now().UTC().Format(time.RFC3339Nano)
	event := registrationEvent(req.Kind, req.RegistryID, id, at, context)
	if err := r.validateEvent(req.Kind, event); err != nil {
		return nil, nil, err
	}
	record := map[string]any{
		"schema_version": "1.0.0", "record_kind": "registry_record", "object_kind": req.Kind,
		"registry_id": req.RegistryID, "parent_id": req.ParentID, "id": id,
		"token_sha256": shaBytes([]byte(req.Token)), "request_sha256": requestHash,
		"context_sha256": context["context_sha256"], "created_at": at,
		"creator_role": "registry", "context": context, "outbox_event": event,
	}
	digest, err := speccontract.SHA(record)
	if err != nil {
		return nil, nil, problem("schema_invalid", "record cannot be canonicalized", err)
	}
	record["record_sha256"] = digest
	data, err := speccontract.Canonical(record)
	if err != nil || len(data) > maxRecordBytes {
		return nil, nil, problem("schema_invalid", "record is invalid or too large", err)
	}
	return record, data, nil
}

func (r *Registry) validateEvent(kind string, event map[string]any) error {
	if err := r.checker.Schemas["event"].Validate(event); err != nil {
		return problem("schema_invalid", "registration event envelope is invalid", err)
	}
	if err := r.checker.PayloadSchemas[kind+".registered"].Validate(event["payload"]); err != nil {
		return problem("schema_invalid", "registration event payload is invalid", err)
	}
	return nil
}

func syncDir(path string) error {
	f, err := os.Open(path)
	if err != nil {
		return err
	}
	defer f.Close()
	return f.Sync()
}

func ensureChild(parent, name string) (string, error) {
	path := filepath.Join(parent, name)
	err := os.Mkdir(path, 0o700)
	if err != nil && !errors.Is(err, os.ErrExist) {
		return "", err
	}
	info, statErr := os.Lstat(path)
	if statErr != nil || !info.IsDir() {
		return "", fmt.Errorf("registry path is not a real directory: %s", path)
	}
	// A peer may have created this directory but not synced its parent yet.
	// Every caller syncs the parent before using the child for publication.
	if err := syncDir(parent); err != nil {
		return "", err
	}
	return path, nil
}

func (r *Registry) bucket(registryID, kind, id string) (string, error) {
	path := r.root
	for _, name := range []string{"registry", shaBytes([]byte(registryID)), kind, id[4:6]} {
		var err error
		path, err = ensureChild(path, name)
		if err != nil {
			return "", problem("registry_unavailable", "cannot prepare registry directory", err)
		}
	}
	return path, nil
}

func (r *Registry) phase(name string) error {
	if r.hook != nil {
		return r.hook(name)
	}
	return nil
}

// Register writes a complete immutable record with its registration event.
// A duplicate returns the actual winner; any changed request for the same ID
// returns conflict. Indeterminate errors must be retried with the same token.
func (r *Registry) Register(req Request) (*Result, error) {
	context, id, requestHash, err := r.prepare(req)
	if err != nil {
		return nil, err
	}
	bucket, err := r.bucket(req.RegistryID, req.Kind, id)
	if err != nil {
		return nil, err
	}
	target := filepath.Join(bucket, id+".json")
	record, data, err := r.buildRecord(req, context, id, requestHash)
	if err != nil {
		return nil, err
	}
	temporary, err := os.CreateTemp(bucket, ".tmp-")
	if err != nil {
		return nil, problem("registry_unavailable", "cannot create temporary record", err)
	}
	tempName := temporary.Name()
	defer os.Remove(tempName) // old crash leftovers are handled by CleanupStaleTemps
	if _, err := temporary.Write(data); err != nil {
		temporary.Close()
		return nil, problem("registry_unavailable", "cannot write temporary record", err)
	}
	if err := r.phase("after_temp_write"); err != nil {
		temporary.Close()
		return nil, problem("registry_unavailable", "injected failure before file sync", err)
	}
	if err := temporary.Sync(); err != nil {
		temporary.Close()
		return nil, problem("registry_unavailable", "cannot sync temporary record", err)
	}
	if err := temporary.Close(); err != nil {
		return nil, problem("registry_unavailable", "cannot close temporary record", err)
	}
	if err := r.phase("after_temp_sync"); err != nil {
		return nil, problem("registry_unavailable", "injected failure before publish", err)
	}
	if err := os.Link(tempName, target); err != nil {
		if !errors.Is(err, os.ErrExist) {
			return nil, problem("indeterminate", "hardlink publication did not report a definite result", err)
		}
		// The winner may still be syncing the directory. Sync it ourselves before
		// reporting success, even when our candidate lost the race.
		if err := syncDir(bucket); err != nil {
			return nil, problem("indeterminate", "cannot sync existing registration", err)
		}
		return r.loadWinner(target, req, id, requestHash, false)
	}
	if err := r.phase("after_link"); err != nil {
		return nil, problem("indeterminate", "publication may be visible but not durable", err)
	}
	if err := syncDir(bucket); err != nil {
		return nil, problem("indeterminate", "cannot sync published registration", err)
	}
	if err := r.phase("after_dir_sync"); err != nil {
		return nil, problem("indeterminate", "published registration was not acknowledged", err)
	}
	if err := os.Remove(tempName); err != nil {
		return nil, problem("indeterminate", "cannot remove published temporary link", err)
	}
	if err := syncDir(bucket); err != nil {
		return nil, problem("indeterminate", "cannot sync temporary cleanup", err)
	}
	return &Result{ID: id, Path: target, Created: true, Record: record}, nil
}

func (r *Registry) loadWinner(path string, req Request, id, requestHash string, created bool) (*Result, error) {
	info, err := os.Lstat(path)
	if err != nil || !info.Mode().IsRegular() || info.Size() > maxRecordBytes {
		return nil, problem("registry_unavailable", "winning registration is missing, unsafe or too large", err)
	}
	f, err := os.Open(path)
	if err != nil {
		return nil, problem("registry_unavailable", "cannot read winning registration", err)
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, maxRecordBytes+1))
	if err != nil || len(data) > maxRecordBytes {
		return nil, problem("registry_unavailable", "cannot read bounded winning registration", err)
	}
	value, err := speccontract.Decode(data)
	if err != nil {
		return nil, problem("corrupt_record", "winning registration is not valid JSON", err)
	}
	record, ok := value.(map[string]any)
	if !ok {
		return nil, problem("corrupt_record", "winning registration is not an object", nil)
	}
	allowed := map[string]bool{
		"schema_version": true, "record_kind": true, "object_kind": true,
		"registry_id": true, "parent_id": true, "id": true,
		"token_sha256": true, "request_sha256": true, "context_sha256": true,
		"created_at": true, "creator_role": true, "context": true,
		"outbox_event": true, "record_sha256": true,
	}
	if len(record) != len(allowed) {
		return nil, problem("corrupt_record", "winning registration field set changed", nil)
	}
	for key := range record {
		if !allowed[key] {
			return nil, problem("corrupt_record", "winning registration has unknown field", nil)
		}
	}
	digest := asString(record["record_sha256"])
	copy := make(map[string]any, len(record))
	for key, item := range record {
		if key != "record_sha256" {
			copy[key] = item
		}
	}
	computed, err := speccontract.SHA(copy)
	if err != nil || digest != computed {
		return nil, problem("corrupt_record", "winning registration digest mismatch", err)
	}
	if asString(record["schema_version"]) != "1.0.0" || asString(record["record_kind"]) != "registry_record" ||
		asString(record["object_kind"]) != req.Kind || asString(record["registry_id"]) != req.RegistryID ||
		(req.ParentID != "" && asString(record["parent_id"]) != req.ParentID) || asString(record["id"]) != id ||
		asString(record["creator_role"]) != "registry" {
		return nil, problem("corrupt_record", "winning registration identity mismatch", nil)
	}
	context, ok := record["context"].(map[string]any)
	if !ok {
		return nil, problem("corrupt_record", "winning context is absent", nil)
	}
	if err := r.checkContext(req.Kind, req.RegistryID, asString(record["parent_id"]), id, asString(record["token_sha256"]), context); err != nil {
		return nil, problem("corrupt_record", "winning context is invalid", err)
	}
	if asString(record["context_sha256"]) != asString(context["context_sha256"]) {
		return nil, problem("corrupt_record", "winning context digest reference mismatch", nil)
	}
	event, ok := record["outbox_event"].(map[string]any)
	if !ok {
		return nil, problem("corrupt_record", "winning outbox event is absent", nil)
	}
	if err := r.validateEvent(req.Kind, event); err != nil {
		return nil, problem("corrupt_record", "winning outbox event is invalid", err)
	}
	expected := registrationEvent(req.Kind, req.RegistryID, id, asString(record["created_at"]), context)
	a, errA := speccontract.Canonical(event)
	b, errB := speccontract.Canonical(expected)
	if errA != nil || errB != nil || !bytes.Equal(a, b) {
		return nil, problem("corrupt_record", "winning outbox does not match context", nil)
	}
	if req.Token != "" && (asString(record["token_sha256"]) != shaBytes([]byte(req.Token)) || asString(record["request_sha256"]) != requestHash) {
		return nil, problem("conflict", "same registration key has different request content", nil)
	}
	return &Result{ID: id, Path: path, Created: created, Record: record}, nil
}

var runIDPattern = regexp.MustCompile(`^run_[0-9a-f]{64}$`)
var attemptIDPattern = regexp.MustCompile(`^att_[0-9a-f]{64}$`)

// Read returns a validated immutable registration by its exact ID. It never
// searches by cycle or selects a latest Run. The caller must separately check
// its parent context and manifest/expectation references.
func (r *Registry) Read(registryID, kind, id string) (*Result, error) {
	if registryID == "" || (kind != "run" && kind != "attempt") ||
		(kind == "run" && !runIDPattern.MatchString(id)) ||
		(kind == "attempt" && !attemptIDPattern.MatchString(id)) {
		return nil, problem("schema_invalid", "invalid registration lookup key", nil)
	}
	bucket := r.root
	for _, part := range []string{"registry", shaBytes([]byte(registryID)), kind, id[4:6]} {
		bucket = filepath.Join(bucket, part)
		info, err := os.Lstat(bucket)
		if err != nil || !info.IsDir() || info.Mode()&os.ModeSymlink != 0 {
			return nil, problem("registry_unavailable", "registration directory unavailable", err)
		}
	}
	return r.loadWinner(filepath.Join(bucket, id+".json"), Request{Kind: kind, RegistryID: registryID}, id, "", false)
}

// CleanupStaleTemps removes only old, unlinked-by-name temporary files in
// this registry namespace. It never removes an authority .json record.
func (r *Registry) CleanupStaleTemps(registryID, kind string, olderThan time.Duration) (int, error) {
	if registryID == "" || (kind != "run" && kind != "attempt") || olderThan <= 0 {
		return 0, problem("schema_invalid", "registry, kind and positive age are required", nil)
	}
	base := filepath.Join(r.root, "registry", shaBytes([]byte(registryID)), kind)
	buckets, err := os.ReadDir(base)
	if errors.Is(err, os.ErrNotExist) {
		return 0, nil
	}
	if err != nil {
		return 0, problem("registry_unavailable", "cannot list registry buckets", err)
	}
	cutoff := time.Now().Add(-olderThan)
	removed := 0
	for _, bucket := range buckets {
		if !bucket.IsDir() {
			continue
		}
		path := filepath.Join(base, bucket.Name())
		entries, err := os.ReadDir(path)
		if err != nil {
			return removed, problem("registry_unavailable", "cannot list temporary records", err)
		}
		published := map[[2]uint64]bool{}
		for _, entry := range entries {
			if !strings.HasSuffix(entry.Name(), ".json") || !entry.Type().IsRegular() {
				continue
			}
			info, err := os.Lstat(filepath.Join(path, entry.Name()))
			if err != nil {
				return removed, problem("registry_unavailable", "cannot inspect authority record", err)
			}
			if stat, ok := info.Sys().(*syscall.Stat_t); ok {
				published[[2]uint64{uint64(stat.Dev), stat.Ino}] = true
			}
		}
		changed := false
		for _, entry := range entries {
			if !strings.HasPrefix(entry.Name(), ".tmp-") || !entry.Type().IsRegular() {
				continue
			}
			candidate := filepath.Join(path, entry.Name())
			info, err := os.Lstat(candidate)
			if errors.Is(err, os.ErrNotExist) {
				continue
			}
			if err != nil {
				return removed, problem("registry_unavailable", "cannot inspect temporary record", err)
			}
			stat, ok := info.Sys().(*syscall.Stat_t)
			if !ok || !info.Mode().IsRegular() || info.ModTime().After(cutoff) ||
				(stat.Nlink != 1 && !published[[2]uint64{uint64(stat.Dev), stat.Ino}]) {
				continue
			}
			if err := os.Remove(candidate); err != nil {
				if errors.Is(err, os.ErrNotExist) {
					continue
				}
				return removed, problem("registry_unavailable", "cannot remove stale temporary record", err)
			}
			removed++
			changed = true
		}
		if changed {
			if err := syncDir(path); err != nil {
				return removed, problem("indeterminate", "cannot sync temporary cleanup", err)
			}
		}
	}
	return removed, nil
}
