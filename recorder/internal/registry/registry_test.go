package registry

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"sledra.local/recorder/internal/speccontract"
)

const (
	fixturePath = "../../../spec/fixtures/1.0.0/valid-bundle.json"
	specPath    = "../../../spec/1.0.0"
	launchToken = "launch-coldstart-20261010"
	execToken   = "exec-member01-first"
)

func fixture(t *testing.T) map[string]any {
	t.Helper()
	data, err := os.ReadFile(fixturePath)
	if err != nil {
		t.Fatal(err)
	}
	value, err := speccontract.Decode(data)
	if err != nil {
		t.Fatal(err)
	}
	return value.(map[string]any)
}

func copyMap(t *testing.T, source map[string]any) map[string]any {
	t.Helper()
	data, err := speccontract.Canonical(source)
	if err != nil {
		t.Fatal(err)
	}
	value, err := speccontract.Decode(data)
	if err != nil {
		t.Fatal(err)
	}
	return value.(map[string]any)
}

func newRegistry(t *testing.T, root string) *Registry {
	t.Helper()
	r, err := New(root, specPath)
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func runRequest(t *testing.T, token string) Request {
	t.Helper()
	context := copyMap(t, fixture(t)["run_context"].(map[string]any))
	context["run_id"] = speccontract.RegistryID(asString(context["registry_id"]), "run", asString(context["deployment_id"]), token)
	context["launch_token_sha256"] = shaBytes([]byte(token))
	requestHash, err := speccontract.SHA(map[string]any{
		"cycle": context["cycle"], "system_id": context["system_id"], "purpose": context["purpose"],
		"deployment_id": context["deployment_id"], "launch_token": token,
	})
	if err != nil {
		t.Fatal(err)
	}
	context["request_sha256"] = requestHash
	context["context_sha256"], err = speccontract.ContentSHA(context, "context_sha256")
	if err != nil {
		t.Fatal(err)
	}
	return Request{Kind: "run", RegistryID: asString(context["registry_id"]), ParentID: asString(context["deployment_id"]), Token: token, Context: context}
}

func attemptRequest(t *testing.T, token string) Request {
	t.Helper()
	context := copyMap(t, fixture(t)["attempt_context"].(map[string]any))
	registryID := asString(fixture(t)["run_context"].(map[string]any)["registry_id"])
	context["attempt_id"] = speccontract.RegistryID(registryID, "attempt", asString(context["task_instance_id"]), token)
	context["execution_token_sha256"] = shaBytes([]byte(token))
	var err error
	context["context_sha256"], err = speccontract.ContentSHA(context, "context_sha256")
	if err != nil {
		t.Fatal(err)
	}
	return Request{Kind: "attempt", RegistryID: registryID, ParentID: asString(context["task_instance_id"]), Token: token, Context: context}
}

func codeOf(t *testing.T, err error) string {
	t.Helper()
	var e *Error
	if !errors.As(err, &e) {
		t.Fatalf("expected registry error, got %v", err)
	}
	return e.Code
}

func TestRegistrationAndOutbox(t *testing.T) {
	r := newRegistry(t, t.TempDir())
	for _, req := range []Request{runRequest(t, launchToken), attemptRequest(t, execToken)} {
		first, err := r.Register(req)
		if err != nil || !first.Created {
			t.Fatalf("initial %s registration: result=%+v error=%v", req.Kind, first, err)
		}
		second, err := r.Register(req)
		if err != nil || second.Created || first.ID != second.ID || first.Path != second.Path {
			t.Fatalf("repeated %s registration: result=%+v error=%v", req.Kind, second, err)
		}
		a, _ := speccontract.Canonical(first.Record)
		b, _ := speccontract.Canonical(second.Record)
		if !bytes.Equal(a, b) {
			t.Fatal("duplicate did not return the actual winning record")
		}
		if first.ID != speccontract.RegistryID(req.RegistryID, req.Kind, req.ParentID, req.Token) {
			t.Fatal("ID differs from SLD-02 golden algorithm")
		}
		event := first.Record["outbox_event"].(map[string]any)
		if event["event_type"] != req.Kind+".registered" || !strings.HasPrefix(asString(event["event_id"]), "evt_") {
			t.Fatal("missing stable registration outbox event")
		}
		if err := r.validateEvent(req.Kind, event); err != nil {
			t.Fatal(err)
		}
		data, err := os.ReadFile(first.Path)
		if err != nil || !bytes.Equal(data, a) {
			t.Fatal("authority record differs from returned canonical winner", err)
		}
		entries, err := os.ReadDir(filepath.Dir(first.Path))
		if err != nil || len(entries) != 1 {
			t.Fatalf("published temporary file was not cleaned: %v %v", entries, err)
		}
	}
}

func TestReadExactRegistration(t *testing.T) {
	root := t.TempDir()
	r := newRegistry(t, root)
	req := runRequest(t, "read-token")
	created, err := r.Register(req)
	if err != nil {
		t.Fatal(err)
	}
	got, err := r.Read(req.RegistryID, "run", created.ID)
	if err != nil || !reflect.DeepEqual(got.Record, created.Record) {
		t.Fatalf("read: %v", err)
	}
	if _, err := r.Read(req.RegistryID, "run", "../../other"); err == nil {
		t.Fatal("unsafe lookup accepted")
	}
	if _, err := r.Read("other-registry", "run", created.ID); err == nil {
		t.Fatal("wrong namespace accepted")
	}
	if _, err := os.Stat(filepath.Join(root, "registry", shaBytes([]byte("other-registry")))); !errors.Is(err, os.ErrNotExist) {
		t.Fatal("read created a registry directory")
	}
}

func concurrentRegistrations(t *testing.T, r *Registry, requests []Request) []*Result {
	t.Helper()
	results := make([]*Result, len(requests))
	errorsByIndex := make([]error, len(requests))
	start := make(chan struct{})
	var group sync.WaitGroup
	for i, req := range requests {
		group.Add(1)
		go func(i int, req Request) {
			defer group.Done()
			<-start
			results[i], errorsByIndex[i] = r.Register(req)
		}(i, req)
	}
	close(start)
	group.Wait()
	for i, err := range errorsByIndex {
		if err != nil {
			t.Fatalf("registration %d: %v", i, err)
		}
	}
	return results
}

func TestHundredSameAndDistinctTokens(t *testing.T) {
	for _, kind := range []string{"run", "attempt"} {
		t.Run(kind, func(t *testing.T) {
			r := newRegistry(t, t.TempDir())
			var request Request
			if kind == "run" {
				request = runRequest(t, launchToken)
			} else {
				request = attemptRequest(t, execToken)
			}
			same := make([]Request, 100)
			for i := range same {
				same[i] = request
			}
			results := concurrentRegistrations(t, r, same)
			winnerBytes, _ := speccontract.Canonical(results[0].Record)
			created := 0
			for _, result := range results {
				if result.Created {
					created++
				}
				data, _ := speccontract.Canonical(result.Record)
				if result.ID != results[0].ID || !bytes.Equal(data, winnerBytes) {
					t.Fatal("same-token contenders did not agree on the winner")
				}
			}
			if created != 1 {
				t.Fatalf("got %d created records, want 1", created)
			}
			different := make([]Request, 100)
			for i := range different {
				token := fmt.Sprintf("distinct-%03d", i)
				if kind == "run" {
					different[i] = runRequest(t, token)
				} else {
					different[i] = attemptRequest(t, token)
				}
			}
			ids := map[string]bool{}
			for _, result := range concurrentRegistrations(t, r, different) {
				if !result.Created || ids[result.ID] {
					t.Fatal("distinct tokens were merged")
				}
				ids[result.ID] = true
			}
		})
	}
}

func TestConflictCorruptionAndInvalidInput(t *testing.T) {
	r := newRegistry(t, t.TempDir())
	req := runRequest(t, launchToken)
	first, err := r.Register(req)
	if err != nil {
		t.Fatal(err)
	}
	changed := runRequest(t, launchToken)
	changed.Context["purpose"] = "different-intent"
	changed.Context["context_sha256"], _ = speccontract.ContentSHA(changed.Context, "context_sha256")
	_, err = r.Register(changed)
	if codeOf(t, err) != "conflict" {
		t.Fatalf("want conflict, got %v", err)
	}
	if _, err := r.Register(req); err != nil {
		t.Fatal("conflict overwrote the winner", err)
	}
	invalid := runRequest(t, launchToken)
	invalid.Context["run_id"] = "run_" + strings.Repeat("0", 64)
	invalid.Context["context_sha256"], _ = speccontract.ContentSHA(invalid.Context, "context_sha256")
	_, err = r.Register(invalid)
	if codeOf(t, err) != "semantic_invalid" {
		t.Fatalf("want semantic_invalid, got %v", err)
	}
	if err := os.WriteFile(first.Path, []byte("{broken"), 0o600); err != nil {
		t.Fatal(err)
	}
	_, err = r.Register(req)
	if codeOf(t, err) != "corrupt_record" {
		t.Fatalf("want corrupt_record, got %v", err)
	}
	data, err := os.ReadFile(first.Path)
	if err != nil || string(data) != "{broken" {
		t.Fatal("corrupt authority was overwritten")
	}
}

func TestFaultClassificationAndRetry(t *testing.T) {
	for _, item := range []struct {
		phase, code string
		published   bool
	}{
		{"after_temp_write", "registry_unavailable", false},
		{"after_temp_sync", "registry_unavailable", false},
		{"after_link", "indeterminate", true},
		{"after_dir_sync", "indeterminate", true},
	} {
		t.Run(item.phase, func(t *testing.T) {
			r := newRegistry(t, t.TempDir())
			req := runRequest(t, launchToken)
			r.hook = func(phase string) error {
				if phase == item.phase {
					return errors.New("synthetic fault")
				}
				return nil
			}
			_, err := r.Register(req)
			if codeOf(t, err) != item.code {
				t.Fatal(err)
			}
			r.hook = nil
			retry, err := r.Register(req)
			if err != nil || retry.Created == item.published {
				t.Fatalf("same-token retry: result=%+v error=%v", retry, err)
			}
		})
	}
}

func TestCrossProcessHelper(t *testing.T) {
	root := os.Getenv("SLD_REGISTRY_PROCESS_ROOT")
	if root == "" {
		return
	}
	r := newRegistry(t, root)
	result, err := r.Register(runRequest(t, launchToken))
	if err != nil {
		t.Fatal(err)
	}
	fmt.Printf("created=%t id=%s\n", result.Created, result.ID)
}

func TestTwentyIndependentProcessesSameToken(t *testing.T) {
	root := t.TempDir()
	var group sync.WaitGroup
	outputs := make([][]byte, 20)
	errorsByIndex := make([]error, 20)
	for i := range outputs {
		group.Add(1)
		go func(i int) {
			defer group.Done()
			cmd := exec.Command(os.Args[0], "-test.run=^TestCrossProcessHelper$")
			cmd.Env = append(os.Environ(), "SLD_REGISTRY_PROCESS_ROOT="+root)
			outputs[i], errorsByIndex[i] = cmd.CombinedOutput()
		}(i)
	}
	group.Wait()
	created := 0
	expected := speccontract.RegistryID("registry-synthetic-1", "run", "wsl-sandbox", launchToken)
	for i, output := range outputs {
		if errorsByIndex[i] != nil || !bytes.Contains(output, []byte("id="+expected)) {
			t.Fatalf("process %d: %s %v", i, output, errorsByIndex[i])
		}
		if bytes.Contains(output, []byte("created=true")) {
			created++
		}
	}
	if created != 1 {
		t.Fatalf("got %d independent process winners, want 1", created)
	}
}

func TestCrashHelper(t *testing.T) {
	phase := os.Getenv("SLD_REGISTRY_CRASH_PHASE")
	if phase == "" {
		return
	}
	r := newRegistry(t, os.Getenv("SLD_REGISTRY_CRASH_ROOT"))
	r.hook = func(name string) error {
		if name == phase {
			os.Exit(97) // no defers; a process crash, not a node/power failure
		}
		return nil
	}
	_, _ = r.Register(runRequest(t, launchToken))
	t.Fatal("fault point was not reached")
}

func TestProcessCrashRecoveryAndStaleCleanup(t *testing.T) {
	for _, item := range []struct {
		phase     string
		published bool
	}{
		{"after_temp_write", false}, {"after_temp_sync", false},
		{"after_link", true}, {"after_dir_sync", true},
	} {
		t.Run(item.phase, func(t *testing.T) {
			root := t.TempDir()
			cmd := exec.Command(os.Args[0], "-test.run=^TestCrashHelper$")
			cmd.Env = append(os.Environ(), "SLD_REGISTRY_CRASH_PHASE="+item.phase, "SLD_REGISTRY_CRASH_ROOT="+root)
			if err := cmd.Run(); err == nil {
				t.Fatal("crash helper unexpectedly returned success")
			} else if exit, ok := err.(*exec.ExitError); !ok || exit.ExitCode() != 97 {
				t.Fatalf("unexpected helper exit: %v", err)
			}
			r := newRegistry(t, root)
			req := runRequest(t, launchToken)
			id := speccontract.RegistryID(req.RegistryID, req.Kind, req.ParentID, req.Token)
			bucket, err := r.bucket(req.RegistryID, req.Kind, id)
			if err != nil {
				t.Fatal(err)
			}
			target := filepath.Join(bucket, id+".json")
			_, statErr := os.Stat(target)
			if item.published != (statErr == nil) {
				t.Fatalf("unexpected authority visibility after %s: %v", item.phase, statErr)
			}
			retry, err := r.Register(req)
			if err != nil || retry.Created == item.published {
				t.Fatalf("same-token crash retry: result=%+v error=%v", retry, err)
			}
			entries, err := os.ReadDir(bucket)
			if err != nil {
				t.Fatal(err)
			}
			stale := 0
			for _, entry := range entries {
				if strings.HasPrefix(entry.Name(), ".tmp-") {
					path := filepath.Join(bucket, entry.Name())
					old := time.Now().Add(-2 * time.Hour)
					if err := os.Chtimes(path, old, old); err != nil {
						t.Fatal(err)
					}
					stale++
				}
			}
			if stale != 1 {
				t.Fatalf("want one crash leftover, got %d", stale)
			}
			removed, err := r.CleanupStaleTemps(req.RegistryID, "run", time.Hour)
			if err != nil || removed != 1 {
				t.Fatalf("stale cleanup: removed=%d error=%v", removed, err)
			}
			if _, err := r.Register(req); err != nil {
				t.Fatal("cleanup damaged the authority", err)
			}
		})
	}
}

func TestGoldenIdentity(t *testing.T) {
	data, err := os.ReadFile("../../../spec/fixtures/1.0.0/golden-vectors.json")
	if err != nil {
		t.Fatal(err)
	}
	v, err := speccontract.Decode(data)
	if err != nil {
		t.Fatal(err)
	}
	golden := v.(map[string]any)
	r := newRegistry(t, t.TempDir())
	for _, item := range []struct {
		req Request
		key string
	}{
		{runRequest(t, launchToken), "run_id"}, {attemptRequest(t, execToken), "attempt_id"},
	} {
		result, err := r.Register(item.req)
		if err != nil || result.ID != golden[item.key] {
			t.Fatalf("%s identity does not match golden vector: %v %v", item.key, result, err)
		}
	}
	sha := sha256.Sum256([]byte(launchToken))
	if hex.EncodeToString(sha[:]) != asString(runRequest(t, launchToken).Context["launch_token_sha256"]) {
		t.Fatal("raw token digest mismatch")
	}
}
