//go:build linux

package execution

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"syscall"
	"testing"
)

type childTrial struct {
	r                           *ChildRecorder
	root, runtime, ref, key, id string
	owner                       childOwner
}

func newChildTrial(t *testing.T) *childTrial {
	t.Helper()
	home, err := os.UserHomeDir()
	if err != nil {
		t.Fatal(err)
	}
	base, err := os.MkdirTemp(filepath.Join(home, ".cache"), "sledra-child-component-")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(base) })
	root := filepath.Join(base, "records")
	runtime := filepath.Join(base, "runtime")
	if err = os.Mkdir(runtime, 0700); err != nil {
		t.Fatal(err)
	}
	config := RootConfig{FormatVersion, LayoutVersion, Namespace{"synthetic", "child-components"}, "+00:00"}
	if err = InitRoot(root, config); err != nil {
		t.Fatal(err)
	}
	r, err := NewChildRecorder()
	if err != nil {
		t.Fatal(err)
	}
	owner := childOwner{uint32(os.Geteuid()), 12345, "1000000", "40000000-0000-4000-8000-000000000001", "4026531836"}
	r.owner = func(pid int, ancestor bool) (childOwner, error) {
		if pid != owner.PID {
			return owner, bad("owner_gone")
		}
		return owner, nil
	}
	raw, err := r.codec.NewStart(nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	event, _ := r.codec.Validate("event", raw)
	p, err := NewChildPrepared(raw, event["occurred_at"].(string))
	if err != nil {
		t.Fatal(err)
	}
	reply := r.Open(root, runtime, owner.PID, p)
	if reply.Status != 0 {
		t.Fatalf("open: %+v", reply)
	}
	return &childTrial{r, root, runtime, reply.Auxiliary, p.OwnerKey, p.ExecutionID, owner}
}
func (tr *childTrial) recorder(t *testing.T) *ChildRecorder {
	t.Helper()
	r, err := NewChildRecorder()
	if err != nil {
		t.Fatal(err)
	}
	r.owner = tr.r.owner
	return r
}
func (tr *childTrial) prepare(t *testing.T, name string, attrs map[string]any) *ChildPrepared {
	t.Helper()
	raw, err := tr.r.codec.NewEvent(tr.id, name, attrs)
	if err != nil {
		t.Fatal(err)
	}
	aux := "-"
	if name == "stage.started" {
		aux = attrs["stage_id"].(string)
	}
	p, err := NewChildPrepared(raw, aux)
	if err != nil {
		t.Fatal(err)
	}
	return p
}
func (tr *childTrial) event(t *testing.T, n int) *ChildPrepared {
	return tr.prepare(t, "synthetic.ready", map[string]any{"step": n})
}
func (tr *childTrial) records(t *testing.T) []byte {
	t.Helper()
	files, err := filepath.Glob(filepath.Join(tr.root, "executions", "*", "*", "*.jsonl"))
	if err != nil || len(files) != 1 {
		t.Fatalf("files %v: %v", files, err)
	}
	raw, err := os.ReadFile(files[0])
	if err != nil {
		t.Fatal(err)
	}
	return raw
}
func (tr *childTrial) state(t *testing.T) *ChildState {
	t.Helper()
	_, _, path, err := ChildReference(tr.ref)
	if err != nil {
		t.Fatal(err)
	}
	raw, err := os.ReadFile(filepath.Join(path, "state.json"))
	if err != nil {
		t.Fatal(err)
	}
	state := &ChildState{}
	if err = ValidateChildControl("state", raw, state); err != nil {
		t.Fatalf("state invalid: %v", err)
	}
	return state
}
func childWant(t *testing.T, r ChildReceipt, status int, code string) {
	t.Helper()
	if r.Status != status || r.Code != code {
		t.Fatalf("want %d/%s: %+v", status, code, r)
	}
}
func TestChildLifecycleRolesContextStagesAndEnd(t *testing.T) {
	tr := newChildTrial(t)
	childWant(t, tr.r.Attach(tr.ref), 0, "ok")
	context := tr.prepare(t, "execution.context", map[string]any{"task": "synthetic.initial", "parameters": map[string]any{"steps": json.Number("9007199254740993")}, "scope": nil, "cycle_kind": "none", "cycle": nil, "run_id": nil})
	before := tr.records(t)
	childWant(t, tr.r.Commit(tr.ref, "event", "", context), 20, "role_denied")
	childWant(t, tr.r.Commit(tr.ref, "owner", "wrong", context), 20, "role_denied")
	if !bytes.Equal(before, tr.records(t)) {
		t.Fatal("role rejection wrote")
	}
	childWant(t, tr.r.Commit(tr.ref, "owner", tr.key, context), 0, "ok")
	p2 := tr.prepare(t, "execution.context", map[string]any{"task": "synthetic.initial", "parameters": map[string]any{"steps": json.Number("9007199254740993")}, "scope": nil, "cycle_kind": "none", "cycle": nil, "run_id": nil})
	before = tr.records(t)
	childWant(t, tr.r.Commit(tr.ref, "owner", tr.key, p2), 0, "same_context")
	if !bytes.Equal(before, tr.records(t)) {
		t.Fatal("context duplicated")
	}
	stageID, _ := NewID()
	stage := tr.prepare(t, "stage.started", map[string]any{"stage": "forecast", "stage_id": stageID})
	childWant(t, tr.r.Commit(tr.ref, "owner", tr.key, stage), 0, "ok")
	childWant(t, tr.r.Commit(tr.ref, "owner", tr.key, stage), 20, "stage_order")
	endStage := tr.prepare(t, "stage.ended", map[string]any{"stage": "forecast", "stage_id": stageID, "exit_code": 0})
	childWant(t, tr.r.Commit(tr.ref, "owner", tr.key, endStage), 0, "ok")
	for i := 0; i < 5; i++ {
		childWant(t, tr.recorder(t).Commit(tr.ref, "event", "", tr.event(t, i)), 0, "ok")
	}
	end := tr.prepare(t, "execution.ended", map[string]any{"exit_code": 7})
	childWant(t, tr.r.Commit(tr.ref, "owner", tr.key, end), 0, "ok")
	before = tr.records(t)
	childWant(t, tr.r.Commit(tr.ref, "owner", tr.key, end), 0, "same_end")
	childWant(t, tr.r.Commit(tr.ref, "owner", tr.key, tr.prepare(t, "execution.ended", map[string]any{"exit_code": 0})), 22, "conflicting_end")
	childWant(t, tr.recorder(t).Commit(tr.ref, "event", "", tr.event(t, 6)), 22, "closed")
	if !bytes.Equal(before, tr.records(t)) {
		t.Fatal("closed execution changed")
	}
}
func TestChildContextConflictAndPersistentDisable(t *testing.T) {
	tr := newChildTrial(t)
	attrs := map[string]any{"task": "one", "parameters": map[string]any{}, "scope": nil, "cycle_kind": "none", "cycle": nil, "run_id": nil}
	childWant(t, tr.r.Commit(tr.ref, "owner", tr.key, tr.prepare(t, "execution.context", attrs)), 0, "ok")
	attrs["task"] = "two"
	childWant(t, tr.r.Commit(tr.ref, "owner", tr.key, tr.prepare(t, "execution.context", attrs)), 23, "conflicting_context")
	before := tr.records(t)
	childWant(t, tr.recorder(t).Commit(tr.ref, "event", "", tr.event(t, 1)), 23, "session_disabled")
	if !bytes.Equal(before, tr.records(t)) {
		t.Fatal("new recorder reopened disabled")
	}
}
func TestChildBusyCloseAdmissionAndExplicitEnd(t *testing.T) {
	tr := newChildTrial(t)
	s, err := tr.r.session(tr.ref)
	if err != nil {
		t.Fatal(err)
	}
	defer s.close()
	if err = s.tryLock(); err != nil {
		t.Fatal(err)
	}
	before := tr.records(t)
	childWant(t, tr.recorder(t).Commit(tr.ref, "event", "", tr.event(t, 1)), 21, "writer_busy")
	end := tr.prepare(t, "execution.ended", map[string]any{"exit_code": 0})
	reply := tr.recorder(t).Commit(tr.ref, "owner", tr.key, end)
	childWant(t, reply, 21, "writer_busy")
	if reply.Phase != "closing" {
		t.Fatal(reply)
	}
	if !bytes.Equal(before, tr.records(t)) {
		t.Fatal("busy wrote")
	}
	_ = syscall.Flock(int(s.lock.Fd()), syscall.LOCK_UN)
	s.held = false
	childWant(t, tr.recorder(t).Commit(tr.ref, "event", "", tr.event(t, 2)), 22, "closing")
	childWant(t, tr.recorder(t).Commit(tr.ref, "owner", tr.key, end), 0, "ok")
	if bytes.Count(tr.records(t), []byte("execution.ended")) != 1 {
		t.Fatal("end not once")
	}
}
func TestChildCloseRacesAdmittedWriter(t *testing.T) {
	tr := newChildTrial(t)
	entered := make(chan struct{})
	release := make(chan struct{})
	producer := tr.recorder(t)
	producer.barrier = func(point string) {
		if point == "admission.accepted" {
			close(entered)
			<-release
		}
	}
	pending := tr.event(t, 1)
	done := make(chan ChildReceipt, 1)
	go func() { done <- producer.Commit(tr.ref, "event", "", pending) }()
	<-entered
	end := tr.prepare(t, "execution.ended", map[string]any{"exit_code": 0})
	childWant(t, tr.recorder(t).Commit(tr.ref, "owner", tr.key, end), 21, "writer_busy")
	close(release)
	childWant(t, <-done, 0, "ok")
	childWant(t, tr.recorder(t).Commit(tr.ref, "owner", tr.key, end), 0, "ok")
	raw := tr.records(t)
	if bytes.Index(raw, []byte("synthetic.ready")) > bytes.Index(raw, []byte("execution.ended")) {
		t.Fatal("event after end")
	}
}
func TestChildNonblockingContentionAndIndependentExecutions(t *testing.T) {
	tr := newChildTrial(t)
	another := newChildTrial(t)
	session, err := tr.r.session(tr.ref)
	if err != nil {
		t.Fatal(err)
	}
	defer session.close()
	if err = session.tryLock(); err != nil {
		t.Fatal(err)
	}
	childWant(t, another.r.Commit(another.ref, "event", "", another.event(t, 1)), 0, "ok")
	_ = syscall.Flock(int(session.lock.Fd()), syscall.LOCK_UN)
	session.held = false
	var wg sync.WaitGroup
	results := make(chan ChildReceipt, 20)
	prepared := make([]*ChildPrepared, 20)
	for i := range prepared {
		prepared[i] = tr.event(t, i)
	}
	for _, p := range prepared {
		wg.Add(1)
		go func(p *ChildPrepared) { defer wg.Done(); results <- tr.recorder(t).Commit(tr.ref, "event", "", p) }(p)
	}
	wg.Wait()
	close(results)
	accepted := 0
	for reply := range results {
		if reply.Status == 0 {
			accepted++
		} else if reply.Status != 21 {
			t.Fatalf("contention poisoned: %+v", reply)
		}
	}
	if accepted == 0 {
		t.Fatal("no accepted writes")
	}
	raw := tr.records(t)
	if bytes.Count(raw, []byte{'\n'}) != accepted+1 {
		t.Fatal("unexpected rows")
	}
	for _, line := range bytes.Split(bytes.TrimSuffix(raw, []byte{'\n'}), []byte{'\n'}) {
		if _, err = tr.r.codec.Validate("event", line); err != nil {
			t.Fatal(err)
		}
	}
}
func TestChildIntentWriteCloseAndCommitFaultsStopNewInstances(t *testing.T) {
	points := []string{"state-intent.before-write", "state-intent.after-write", "state-intent.close", "state-intent.before-rename", "state-intent.after-rename", "record.before-open", "record.before-write", "record.after-write", "record.after-close", "state-commit.before-write", "state-commit.after-write", "state-commit.close", "state-commit.before-rename", "state-commit.after-rename"}
	for _, point := range points {
		t.Run(point, func(t *testing.T) {
			tr := newChildTrial(t)
			p := tr.event(t, 1)
			fired := false
			tr.r.fault = func(at string) error {
				if at == point && !fired {
					fired = true
					return errors.New("injected")
				}
				return nil
			}
			reply := tr.r.Commit(tr.ref, "event", "", p)
			childWant(t, reply, 23, "indeterminate")
			if !fired {
				t.Fatal("fault not reached")
			}
			before := tr.records(t)
			childWant(t, tr.recorder(t).Commit(tr.ref, "event", "", tr.event(t, 2)), 23, "session_disabled")
			if !bytes.Equal(before, tr.records(t)) {
				t.Fatal("failed session reopened")
			}
			lookup := tr.recorder(t).Lookup(tr.ref, p)
			if point == "state-commit.after-rename" {
				childWant(t, lookup, 0, "ok")
				if lookup.Phase != "disabled" {
					t.Fatal(lookup)
				}
			} else {
				childWant(t, lookup, 24, "result_unknown")
			}
		})
	}
}
func TestChildShortWriteAndCloseFailureNeverRepair(t *testing.T) {
	for _, fault := range []string{"short", "full-error", "close"} {
		t.Run(fault, func(t *testing.T) {
			tr := newChildTrial(t)
			writes := 0
			tr.r.writerHook = func(w *Writer) {
				original := w.write
				w.write = func(raw []byte) (int, error) {
					writes++
					if fault == "short" {
						return original(raw[:5])
					}
					n, err := original(raw)
					if fault == "full-error" {
						return n, io.ErrUnexpectedEOF
					}
					return n, err
				}
				if fault == "close" {
					close := w.closeFile
					w.closeFile = func() error { _ = close(); return io.ErrUnexpectedEOF }
				}
			}
			childWant(t, tr.r.Commit(tr.ref, "event", "", tr.event(t, 1)), 23, "indeterminate")
			before := tr.records(t)
			childWant(t, tr.recorder(t).Commit(tr.ref, "event", "", tr.event(t, 2)), 23, "session_disabled")
			if writes != 1 || !bytes.Equal(before, tr.records(t)) {
				t.Fatal("retry/repair after uncertainty")
			}
		})
	}
}
func TestChildAbandonedWritingAndEndIntentCannotResume(t *testing.T) {
	for _, kind := range []string{"event", "end"} {
		t.Run(kind, func(t *testing.T) {
			tr := newChildTrial(t)
			p := tr.event(t, 1)
			if kind == "end" {
				p = tr.prepare(t, "execution.ended", map[string]any{"exit_code": 0})
			}
			s, err := tr.r.session(tr.ref)
			if err != nil {
				t.Fatal(err)
			}
			if err = s.tryLock(); err != nil {
				t.Fatal(err)
			}
			if err = s.loadState(); err != nil {
				t.Fatal(err)
			}
			s.state.Phase = "writing"
			s.state.Pending = childPending(p)
			_ = childAdvance(s.state)
			if err = s.save("test"); err != nil {
				t.Fatal(err)
			}
			s.close()
			before := tr.records(t)
			childWant(t, tr.recorder(t).Commit(tr.ref, "event", "", tr.event(t, 2)), 23, "stale_writing")
			if !bytes.Equal(before, tr.records(t)) {
				t.Fatal("replayed abandoned intent")
			}
		})
	}
}
func TestChildReceiptLookupIsReadOnlyAndBoundedLastSlot(t *testing.T) {
	tr := newChildTrial(t)
	p := tr.event(t, 1)
	childWant(t, tr.r.Commit(tr.ref, "event", "", p), 0, "ok")
	before := tr.records(t)
	childWant(t, tr.recorder(t).Lookup(tr.ref, p), 0, "ok")
	if !bytes.Equal(before, tr.records(t)) {
		t.Fatal("lookup appended")
	}
	childWant(t, tr.r.Commit(tr.ref, "event", "", tr.event(t, 2)), 0, "ok")
	childWant(t, tr.recorder(t).Lookup(tr.ref, p), 24, "result_unknown")
	childWant(t, tr.recorder(t).Disable(tr.ref), 23, "session_disabled")
	childWant(t, tr.recorder(t).Commit(tr.ref, "event", "", tr.event(t, 3)), 23, "session_disabled")
}
func TestChildDisableDuringWriteHasRequestedThenQuiescentScope(t *testing.T) {
	tr := newChildTrial(t)
	entered := make(chan struct{})
	release := make(chan struct{})
	writer := tr.recorder(t)
	writer.barrier = func(point string) {
		if point == "record.after-write" {
			close(entered)
			<-release
		}
	}
	p := tr.event(t, 1)
	done := make(chan ChildReceipt, 1)
	go func() { done <- writer.Commit(tr.ref, "event", "", p) }()
	<-entered
	stop := tr.recorder(t).Disable(tr.ref)
	childWant(t, stop, 23, "session_disabled")
	if stop.Stop != "requested" {
		t.Fatal(stop)
	}
	close(release)
	stopped := <-done
	if stopped.Stop != "quiescent" || stopped.Status != 23 {
		t.Fatal(stopped)
	}
	before := tr.records(t)
	childWant(t, tr.recorder(t).Commit(tr.ref, "event", "", tr.event(t, 2)), 23, "session_disabled")
	if !bytes.Equal(before, tr.records(t)) {
		t.Fatal("new write after stop")
	}
	childWant(t, tr.recorder(t).Lookup(tr.ref, p), 0, "ok")
}
func TestChildStopPublicationFailureReportsActualScope(t *testing.T) {
	for _, both := range []bool{false, true} {
		t.Run(map[bool]string{false: "state-fallback", true: "both-fail"}[both], func(t *testing.T) {
			tr := newChildTrial(t)
			tr.r.fault = func(point string) error {
				if point == "disabled.publish" || both && strings.HasPrefix(point, "state-disable.") {
					return errors.New("injected")
				}
				return nil
			}
			stop := tr.r.Disable(tr.ref)
			if both {
				childWant(t, stop, 23, "disable_publish_failed")
				if stop.Stop != "local" {
					t.Fatal(stop)
				}
				if tr.state(t).Phase != "active" {
					t.Fatal("pretended durable stop")
				}
			} else {
				childWant(t, stop, 23, "session_disabled")
				if stop.Stop != "quiescent" {
					t.Fatal(stop)
				}
				childWant(t, tr.recorder(t).Commit(tr.ref, "event", "", tr.event(t, 1)), 23, "session_disabled")
			}
		})
	}
}
func TestChildMissingCorruptReplacedAndWrongIdentityNeverRebuild(t *testing.T) {
	cases := []string{"state-missing", "state-corrupt", "descriptor-missing", "lock-missing", "lock-replaced", "record-missing", "wrong-boot", "wrong-start", "wrong-namespace", "wrong-uid", "owner-dead"}
	for _, name := range cases {
		t.Run(name, func(t *testing.T) {
			tr := newChildTrial(t)
			_, _, dir, _ := ChildReference(tr.ref)
			switch name {
			case "state-missing":
				os.Remove(filepath.Join(dir, "state.json"))
			case "state-corrupt":
				os.WriteFile(filepath.Join(dir, "state.json"), []byte("broken"), 0600)
			case "descriptor-missing":
				os.Remove(filepath.Join(dir, "event-context.json"))
			case "lock-missing":
				os.Remove(filepath.Join(dir, "writer.lock"))
			case "lock-replaced":
				os.Rename(filepath.Join(dir, "writer.lock"), filepath.Join(dir, "old.lock"))
				os.WriteFile(filepath.Join(dir, "writer.lock"), nil, 0600)
			case "record-missing":
				files, _ := filepath.Glob(filepath.Join(tr.root, "executions", "*", "*", "*.jsonl"))
				os.Remove(files[0])
			default:
				tr.r.owner = func(int, bool) (childOwner, error) {
					owner := tr.owner
					switch name {
					case "wrong-boot":
						owner.Boot = "00000000-0000-4000-8000-000000000000"
					case "wrong-start":
						owner.Start = "1"
					case "wrong-namespace":
						owner.Namespace = "1"
					case "wrong-uid":
						owner.UID++
					case "owner-dead":
						return owner, bad("owner_gone")
					}
					return owner, nil
				}
			}
			reply := tr.r.Commit(tr.ref, "event", "", tr.event(t, 1))
			if reply.Status != 23 && reply.Status != 26 {
				t.Fatal(reply)
			}
			if name == "state-missing" {
				if _, err := os.Stat(filepath.Join(dir, "state.json")); !os.IsNotExist(err) {
					t.Fatal("state rebuilt")
				}
			}
			if name == "record-missing" {
				files, _ := filepath.Glob(filepath.Join(tr.root, "executions", "*", "*", "*.jsonl"))
				if len(files) != 0 {
					t.Fatal("record rebuilt")
				}
			}
		})
	}
}

func TestChildInitializationFailuresNeverPublishOrRetryStart(t *testing.T) {
	for _, point := range []string{"state-create.before-write", "state-intent.before-rename", "record.before-write", "record.after-close", "state-commit.before-rename", "descriptor-publish.before-write", "descriptor-publish.before-rename", "descriptor-publish.after-rename"} {
		t.Run(point, func(t *testing.T) {
			tr := newChildTrial(t)
			raw, err := tr.r.codec.NewStart(nil, nil)
			if err != nil {
				t.Fatal(err)
			}
			event, _ := tr.r.codec.Validate("event", raw)
			p, err := NewChildPrepared(raw, event["occurred_at"].(string))
			if err != nil {
				t.Fatal(err)
			}
			tr.r.fault = func(at string) error {
				if at == point {
					return errors.New("injected")
				}
				return nil
			}
			reply := tr.r.Open(tr.root, tr.runtime, tr.owner.PID, p)
			if reply.Status != 23 || reply.Auxiliary != "-" {
				t.Fatalf("published failed reference: %+v", reply)
			}
			before := map[string][]byte{}
			files, _ := filepath.Glob(filepath.Join(tr.root, "executions", "*", "*", "*.jsonl"))
			for _, file := range files {
				before[file], err = os.ReadFile(file)
				if err != nil {
					t.Fatal(err)
				}
			}
			retry := tr.recorder(t).Open(tr.root, tr.runtime, tr.owner.PID, p)
			if retry.Status == 0 || retry.Auxiliary != "-" {
				t.Fatal("reused failed session")
			}
			files, _ = filepath.Glob(filepath.Join(tr.root, "executions", "*", "*", "*.jsonl"))
			if len(files) != len(before) {
				t.Fatal("retried start")
			}
			for _, file := range files {
				after, _ := os.ReadFile(file)
				if !bytes.Equal(before[file], after) {
					t.Fatal("rewrote start")
				}
			}
		})
	}
}
func TestChildRuntimePermissionsLinksAndRootSeparation(t *testing.T) {
	for _, scenario := range []string{"relative", "missing", "permissions", "symlink", "root-overlap", "state-symlink", "descriptor-symlink", "lock-symlink", "state-oversized", "closing-oversized"} {
		t.Run(scenario, func(t *testing.T) {
			tr := newChildTrial(t)
			_, _, session, _ := ChildReference(tr.ref)
			raw, err := tr.r.codec.NewStart(nil, nil)
			if err != nil {
				t.Fatal(err)
			}
			event, _ := tr.r.codec.Validate("event", raw)
			p, _ := NewChildPrepared(raw, event["occurred_at"].(string))
			runtime := tr.runtime
			switch scenario {
			case "relative":
				runtime = "relative"
			case "missing":
				runtime = filepath.Join(tr.runtime, "absent")
			case "permissions":
				os.Chmod(tr.runtime, 0755)
			case "symlink":
				runtime = filepath.Join(filepath.Dir(tr.runtime), "link")
				os.Symlink(tr.runtime, runtime)
			case "root-overlap":
				runtime = filepath.Join(tr.root, "runtime")
				os.Mkdir(runtime, 0700)
			default:
				name := strings.Split(scenario, "-")[0] + ".json"
				if strings.HasPrefix(scenario, "lock") {
					name = "writer.lock"
				}
				if strings.HasPrefix(scenario, "descriptor") {
					name = "event-context.json"
				}
				if strings.HasPrefix(scenario, "closing") {
					name = "closing"
				}
				path := filepath.Join(session, name)
				if strings.HasSuffix(scenario, "symlink") {
					os.Rename(path, path+".old")
					os.Symlink(path+".old", path)
				} else {
					limit := ChildStateLimit
					if name == "closing" {
						limit = ChildClosingLimit
					}
					os.WriteFile(path, bytes.Repeat([]byte{' '}, limit+1), 0600)
				}
				before := tr.records(t)
				reply := tr.r.Commit(tr.ref, "event", "", tr.event(t, 1))
				if reply.Status != 23 && reply.Status != 26 {
					t.Fatal(reply)
				}
				if !bytes.Equal(before, tr.records(t)) {
					t.Fatal("unsafe control wrote data")
				}
				return
			}
			reply := tr.r.Open(tr.root, runtime, tr.owner.PID, p)
			if reply.Status == 0 || reply.Auxiliary != "-" {
				t.Fatal(reply)
			}
		})
	}
}
func TestChildLookupBusyIsReadOnlyThenCallerDisablesOnce(t *testing.T) {
	tr := newChildTrial(t)
	p := tr.event(t, 1)
	childWant(t, tr.r.Commit(tr.ref, "event", "", p), 0, "ok")
	session, err := tr.r.session(tr.ref)
	if err != nil {
		t.Fatal(err)
	}
	defer session.close()
	if err = session.tryLock(); err != nil {
		t.Fatal(err)
	}
	before := tr.records(t)
	childWant(t, tr.recorder(t).Lookup(tr.ref, p), 24, "result_unknown")
	_, _, path, _ := ChildReference(tr.ref)
	if _, err = os.Stat(filepath.Join(path, "disabled")); !os.IsNotExist(err) {
		t.Fatal("read-only lookup published stop")
	}
	stopped := tr.recorder(t).Disable(tr.ref)
	if stopped.Stop != "requested" {
		t.Fatal(stopped)
	}
	_ = syscall.Flock(int(session.lock.Fd()), syscall.LOCK_UN)
	session.held = false
	childWant(t, tr.recorder(t).Commit(tr.ref, "event", "", tr.event(t, 2)), 23, "session_disabled")
	if !bytes.Equal(before, tr.records(t)) {
		t.Fatal("lookup/disable appended")
	}
}

func TestChildEquivalentContextKeepsNanosecondsAndExactDecimals(t *testing.T) {
	tr := newChildTrial(t)
	attrs := map[string]any{"task": "synthetic.initial", "parameters": map[string]any{"exact": json.Number("9007199254740993")}, "scope": nil, "cycle_kind": "single", "cycle": "2026-10-30T08:00:00.000000001+08:00", "run_id": nil}
	childWant(t, tr.r.Commit(tr.ref, "owner", tr.key, tr.prepare(t, "execution.context", attrs)), 0, "ok")
	before := tr.records(t)
	attrs["cycle"] = "2026-10-30T00:00:00.000000001Z"
	attrs["parameters"] = map[string]any{"exact": json.Number("9007199254740993.0")}
	childWant(t, tr.r.Commit(tr.ref, "owner", tr.key, tr.prepare(t, "execution.context", attrs)), 0, "same_context")
	if !bytes.Equal(before, tr.records(t)) {
		t.Fatal("equivalent context appended")
	}
	attrs["cycle"] = "2026-10-30T00:00:00.000000002Z"
	childWant(t, tr.r.Commit(tr.ref, "owner", tr.key, tr.prepare(t, "execution.context", attrs)), 23, "conflicting_context")
	if !bytes.Equal(before, tr.records(t)) {
		t.Fatal("conflicting context appended")
	}
}

func TestChildBindingAndConcurrentDiagnosticBudget(t *testing.T) {
	tr := newChildTrial(t)
	before := tr.records(t)
	reply, id := tr.r.Bind(tr.ref)
	childWant(t, reply, 0, "ok")
	if id != tr.id || !bytes.Equal(before, tr.records(t)) {
		t.Fatal("binding changed facts or identity")
	}
	const count = 12
	var wg sync.WaitGroup
	var mu sync.Mutex
	winners := 0
	for range count {
		recorder := tr.recorder(t)
		wg.Add(1)
		go func() {
			defer wg.Done()
			claimed, err := recorder.ClaimDiagnostic(tr.ref)
			mu.Lock()
			defer mu.Unlock()
			if err != nil {
				t.Error(err)
			}
			if claimed {
				winners++
			}
		}()
	}
	wg.Wait()
	if winners != 1 {
		t.Fatalf("diagnostic winners: %d", winners)
	}
	if !bytes.Equal(before, tr.records(t)) {
		t.Fatal("diagnostic appended")
	}
	if tr.state(t).Revision != 2 {
		t.Fatal("diagnostic changed writer state")
	}
	_, _, path, _ := ChildReference(tr.ref)
	if err := os.WriteFile(filepath.Join(path, "diagnostic"), []byte("corrupt"), 0600); err != nil {
		t.Fatal(err)
	}
	if _, err := tr.r.ClaimDiagnostic(tr.ref); err == nil {
		t.Fatal("corrupt latch treated as suppressed")
	}
	if err := os.Remove(filepath.Join(path, "state.json")); err != nil {
		t.Fatal(err)
	}
	if _, err := tr.r.ClaimDiagnostic(tr.ref); err == nil {
		t.Fatal("missing control claimed shared diagnostic")
	}
}
