package context

import (
	"bytes"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync"
	"testing"

	"github.com/cemc-oper/sledra/recorder/event"
	"github.com/cemc-oper/sledra/recorder/internal/speccontract"
)

const specPath = "../../spec/1.0.0"
const fixturePath = "../../spec/fixtures/1.0.0/valid-bundle.json"

func fixture(t *testing.T) map[string]any {
	t.Helper()
	b, err := os.ReadFile(fixturePath)
	if err != nil {
		t.Fatal(err)
	}
	v, err := speccontract.Decode(b)
	if err != nil {
		t.Fatal(err)
	}
	return v.(map[string]any)
}
func setup(t *testing.T) (*Service, string, map[string]any) {
	t.Helper()
	root := t.TempDir()
	s, err := New(root, specPath)
	if err != nil {
		t.Fatal(err)
	}
	return s, root, fixture(t)
}
func runRequest(b map[string]any, token string) RunRequest {
	r := b["run_context"].(map[string]any)
	return RunRequest{RegistryID: str(r["registry_id"]), LaunchToken: token, SystemID: str(r["system_id"]), Purpose: str(r["purpose"]), DeploymentID: str(r["deployment_id"]), PlatformID: str(r["platform_id"]), Cycle: obj(r["cycle"]), Definition: obj(b["definition_manifest"]), Expectation: obj(b["expectation"]), Scope: []ScopeInput{{TaskKey: "main.forecast", Parameters: map[string]any{"member": "01", "lead_seconds": strNumber(0)}}, {TaskKey: "main.forecast", Parameters: map[string]any{"member": "02", "lead_seconds": strNumber(0)}}}}
}
func strNumber(n int) any { return json.Number(strconv.Itoa(n)) }
func attemptRequest(run map[string]any, token string, retry *string) AttemptRequest {
	return AttemptRequest{Run: run, TaskKey: "main.forecast", Parameters: map[string]any{"member": "01", "lead_seconds": strNumber(0)}, ExecutionToken: token, RetryOf: retry, ProcessRole: "forecast_program"}
}
func code(t *testing.T, err error, want string) {
	t.Helper()
	var e *Error
	if !errors.As(err, &e) || e.Code != want {
		t.Fatalf("got %v; want %s", err, want)
	}
}

func TestRunAttemptRetryAndFinish(t *testing.T) {
	s, root, b := setup(t)
	r1, err := s.CreateRun(runRequest(b, "launch-1"))
	if err != nil || !r1.Created {
		t.Fatalf("run1: %+v %v", r1, err)
	}
	r1Again, err := s.CreateRun(runRequest(b, "launch-1"))
	if err != nil || r1Again.Created || !equal(r1.Context, r1Again.Context) {
		t.Fatalf("run retry: %+v %v", r1Again, err)
	}
	r2, err := s.CreateRun(runRequest(b, "launch-2"))
	if err != nil || r2.ID == r1.ID {
		t.Fatalf("same-cycle runs merged: %+v %v", r2, err)
	}
	rerunRequest := runRequest(b, "launch-rerun")
	rerunRequest.RerunOf = &r1.ID
	rerun, err := s.CreateRun(rerunRequest)
	if err != nil || rerun.ID == r1.ID || rerun.Context["rerun_of"] != r1.ID {
		t.Fatalf("explicit rerun: %+v %v", rerun, err)
	}
	runPath := filepath.Join(root, "contexts", "run.json")
	if err := s.PublishContext(runPath, r1.Context); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(runPath)
	if err != nil || info.Mode().Perm() != 0400 {
		t.Fatalf("context mode: %v %v", info, err)
	}
	loaded, err := s.LoadContext(runPath)
	if err != nil || !equal(loaded, r1.Context) {
		t.Fatalf("load run: %v", err)
	}
	a1, err := s.CreateAttempt(attemptRequest(loaded, "execute-1", nil))
	if err != nil {
		t.Fatal(err)
	}
	if a1.ID == r1.ID || a1.Context["retry_of"] != nil {
		t.Fatal("invalid first attempt")
	}
	a1Again, err := s.CreateAttempt(attemptRequest(loaded, "execute-1", nil))
	if err != nil || a1Again.Created || !equal(a1.Context, a1Again.Context) {
		t.Fatalf("attempt retry: %v", err)
	}
	a2, err := s.CreateAttempt(attemptRequest(loaded, "execute-2", &a1.ID))
	if err != nil || a2.ID == a1.ID || a2.Context["retry_of"] != a1.ID {
		t.Fatalf("second attempt: %+v %v", a2, err)
	}
	codec, err := event.New("process_owner")
	if err != nil {
		t.Fatal(err)
	}
	for _, fact := range []map[string]any{syntheticExit(a1, "b36a18ef-9696-4a8e-a324-4be432761012", 1, "2026-10-10T03:12:14Z"), syntheticExit(a2, "b36a18ef-9696-4a8e-a324-4be432761013", 0, "2026-10-10T03:15:14Z")} {
		data, err := speccontract.Canonical(fact)
		if err != nil {
			t.Fatal(err)
		}
		if _, err := codec.Validate(data); err != nil {
			t.Fatal("A1/A2 exit fixture", err)
		}
	}
	// A branch is retained, never silently selected as the latest success.
	a2Branch, err := s.CreateAttempt(attemptRequest(loaded, "execute-2b", &a1.ID))
	if err != nil || a2Branch.ID == a2.ID {
		t.Fatalf("branch: %+v %v", a2Branch, err)
	}
	f := FinishRequest{Run: loaded, DeclarationID: "finish-1", RuleVersion: "rule-1", CoveredAttemptIDs: []string{a2.ID, a1.ID}, Reason: "synthetic end", Result: "finished", SourceID: "unit-test", SessionID: "finish-session"}
	first, err := s.Finish(f)
	if err != nil || !first.Created {
		t.Fatalf("finish: %+v %v", first, err)
	}
	before, err := os.ReadFile(first.Path)
	if err != nil {
		t.Fatal(err)
	}
	a3, err := s.CreateAttempt(attemptRequest(loaded, "execute-3", &a2.ID))
	if err != nil || a3.Context["run_id"] != r1.ID {
		t.Fatalf("after finish: %+v %v", a3, err)
	}
	second, err := s.Finish(f)
	if err != nil || second.Created || !equal(first.Event, second.Event) {
		t.Fatalf("finish retry: %+v %v", second, err)
	}
	after, err := os.ReadFile(first.Path)
	if err != nil || !bytes.Equal(before, after) {
		t.Fatal("finish changed after new attempt")
	}
	late := syntheticExit(a1, "69450c98-3a1f-4a25-8ff2-1b93c346252e", 1, "2026-10-11T03:12:14Z")
	lateBytes, err := speccontract.Canonical(late)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := codec.Validate(lateBytes); err != nil {
		t.Fatal("late fixture", err)
	}
	if !bytes.Equal(before, mustRead(t, first.Path)) {
		t.Fatal("late old event changed finish")
	}
	covered := list(obj(first.Event["payload"])["covered_attempt_ids"])
	if len(covered) != 2 || covered[0] != a1.ID && covered[0] != a2.ID {
		t.Fatal("covered set lost")
	}
	if strings.Contains(string(before), "execute-3") || strings.Contains(string(before), a3.ID) {
		t.Fatal("new attempt inserted into old declaration")
	}
	// A later declaration may cover the new Attempt without replacing F1.
	f.DeclarationID = "finish-2"
	f.CoveredAttemptIDs = []string{a1.ID, a2.ID, a3.ID}
	third, err := s.Finish(f)
	if err != nil || third.Path == first.Path {
		t.Fatalf("later declaration: %+v %v", third, err)
	}
}

func TestConflictsAndMissingParents(t *testing.T) {
	s, root, b := setup(t)
	r, err := s.CreateRun(runRequest(b, "launch-1"))
	if err != nil {
		t.Fatal(err)
	}
	changed := runRequest(b, "launch-1")
	changed.Purpose = "experiment"
	_, err = s.CreateRun(changed)
	code(t, err, "parent_mismatch") // expectation does not match
	changed = runRequest(b, "launch-1")
	changed.PlatformID = "different"
	_, err = s.CreateRun(changed)
	code(t, err, "conflict")
	bad := attemptRequest(r.Context, "execution-1", nil)
	bad.TaskKey = "unknown"
	_, err = s.CreateAttempt(bad)
	code(t, err, "parent_mismatch")
	bad = attemptRequest(r.Context, "execution-1", nil)
	bad.Parameters["undeclared"] = "x"
	_, err = s.CreateAttempt(bad)
	code(t, err, "semantic_invalid")
	bad = attemptRequest(r.Context, "execution-1", nil)
	delete(bad.Parameters, "lead_seconds")
	_, err = s.CreateAttempt(bad)
	code(t, err, "semantic_invalid")
	bad = attemptRequest(r.Context, "execution-1", nil)
	bad.Parameters["lead_seconds"] = "0"
	_, err = s.CreateAttempt(bad)
	code(t, err, "semantic_invalid")
	a1, err := s.CreateAttempt(attemptRequest(r.Context, "execution-1", nil))
	if err != nil {
		t.Fatal(err)
	}
	other, err := s.CreateRun(runRequest(b, "launch-2"))
	if err != nil {
		t.Fatal(err)
	}
	bad = attemptRequest(other.Context, "execution-2", &a1.ID)
	_, err = s.CreateAttempt(bad)
	code(t, err, "parent_mismatch")
	bad = attemptRequest(r.Context, "execution-1", &a1.ID)
	_, err = s.CreateAttempt(bad)
	code(t, err, "semantic_invalid")
	path := filepath.Join(root, "contexts", "attempt.json")
	if err := s.PublishContext(path, a1.Context); err != nil {
		t.Fatal(err)
	}
	if _, err := s.LoadContext(path); err != nil {
		t.Fatal(err)
	}
	if err := os.Chmod(path, 0600); err != nil {
		t.Fatal(err)
	}
	changedBytes := bytes.ReplaceAll(mustRead(t, path), []byte("forecast_program"), []byte("other_program"))
	if err := os.WriteFile(path, changedBytes, 0600); err != nil {
		t.Fatal(err)
	}
	_, err = s.LoadContext(path)
	code(t, err, "context_invalid")
	tampered := map[string]any{}
	for k, v := range a1.Context {
		tampered[k] = v
	}
	tampered["process_role"] = "tampered"
	_, err = s.ValidateAttempt(tampered)
	code(t, err, "context_invalid")
	if err := os.Remove(path); err != nil {
		t.Fatal(err)
	}
	_, err = s.LoadContext(path)
	code(t, err, "context_missing")
	_, err = s.LoadContext("/tmp/no-context.json")
	code(t, err, "path_invalid")
	f := FinishRequest{Run: r.Context, DeclarationID: "finish-1", RuleVersion: "rule-1", CoveredAttemptIDs: []string{a1.ID}, Reason: "done", Result: "finished", SourceID: "unit-test", SessionID: "session"}
	first, err := s.Finish(f)
	if err != nil {
		t.Fatal(err)
	}
	f.Reason = "changed"
	_, err = s.Finish(f)
	code(t, err, "conflict")
	if _, err := os.Stat(first.Path); err != nil {
		t.Fatal(err)
	}
}

func mustRead(t *testing.T, path string) []byte {
	t.Helper()
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return b
}

func syntheticExit(attempt *Result, id string, code int, recorded string) map[string]any {
	return map[string]any{"schema_version": "1.0.0", "kind": "event", "event_id": id, "event_type": "attempt.exited", "producer": map[string]any{"name": "synthetic", "version": "1", "role": "process_owner"}, "source": map[string]any{"kind": "direct", "source_id": "synthetic-node", "session_id": attempt.ID}, "occurred_at": "2026-10-10T03:12:14Z", "recorded_at": recorded, "time_quality": map[string]any{"basis": "producer_clock"}, "association": map[string]any{"run_id": attempt.Context["run_id"], "task_instance_id": attempt.Context["task_instance_id"], "attempt_id": attempt.ID}, "payload": map[string]any{"termination": "exited", "exit_code": json.Number(strconv.Itoa(code)), "process_role": "forecast_program"}}
}

func TestConcurrentSameFinishDeclaration(t *testing.T) {
	s, _, b := setup(t)
	r, err := s.CreateRun(runRequest(b, "finish-race"))
	if err != nil {
		t.Fatal(err)
	}
	f := FinishRequest{Run: r.Context, DeclarationID: "same", RuleVersion: "1", CoveredAttemptIDs: []string{}, Reason: "none started", Result: "abandoned", SourceID: "unit-test", SessionID: "session"}
	const n = 12
	results := make([]*FinishResult, n)
	errs := make([]error, n)
	start := make(chan struct{})
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func(i int) { defer wg.Done(); <-start; results[i], errs[i] = s.Finish(f) }(i)
	}
	close(start)
	wg.Wait()
	created := 0
	for i, err := range errs {
		if err != nil {
			t.Fatalf("finish %d: %v", i, err)
		}
		if results[i].Created {
			created++
		}
		if results[i].Path != results[0].Path || !equal(results[i].Event, results[0].Event) {
			t.Fatal("finish winner differed")
		}
	}
	if created != 1 {
		t.Fatalf("created=%d", created)
	}
}

func TestConcurrentFinishAndLateAttempt(t *testing.T) {
	s, _, b := setup(t)
	r, err := s.CreateRun(runRequest(b, "launch"))
	if err != nil {
		t.Fatal(err)
	}
	a1, err := s.CreateAttempt(attemptRequest(r.Context, "a1", nil))
	if err != nil {
		t.Fatal(err)
	}
	f := FinishRequest{Run: r.Context, DeclarationID: "finish", RuleVersion: "1", CoveredAttemptIDs: []string{a1.ID}, Reason: "snapshot", Result: "finished", SourceID: "unit-test", SessionID: "session"}
	var wg sync.WaitGroup
	wg.Add(2)
	var finish *FinishResult
	var a2 *Result
	var e1, e2 error
	go func() { defer wg.Done(); finish, e1 = s.Finish(f) }()
	go func() { defer wg.Done(); a2, e2 = s.CreateAttempt(attemptRequest(r.Context, "a2", &a1.ID)) }()
	wg.Wait()
	if e1 != nil || e2 != nil {
		t.Fatalf("finish/attempt %v %v", e1, e2)
	}
	if a2.Context["run_id"] != r.ID || len(list(obj(finish.Event["payload"])["covered_attempt_ids"])) != 1 {
		t.Fatal("concurrent attempt changed snapshot")
	}
}
