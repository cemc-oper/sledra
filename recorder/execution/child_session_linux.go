//go:build linux

package execution

import (
	"bytes"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"syscall"
)

// ChildRecorder coordinates one operation at a time; it never launches business work.
// A new instance/CLI still validates the same persistent stop state on every call.
type ChildRecorder struct {
	codec      *Codec
	owner      func(int, bool) (childOwner, error)
	fault      func(string) error
	barrier    func(string)
	writerHook func(*Writer)
}

func NewChildRecorder() (*ChildRecorder, error) {
	if !ChildSupported() {
		return nil, bad("installation_incompatible")
	}
	c, err := NewCodec()
	if err != nil {
		return nil, err
	}
	if err = loadChildSchemas(); err != nil {
		return nil, err
	}
	return &ChildRecorder{codec: c, owner: childIdentity}, nil
}
func (r *ChildRecorder) cut(point string) error {
	if r.barrier != nil {
		r.barrier(point)
	}
	if r.fault != nil {
		return r.fault(point)
	}
	return nil
}

type childSession struct {
	r             *ChildRecorder
	dir           *os.Root
	lock          *os.File
	descriptor    ChildDescriptor
	descriptorRaw []byte
	state         *ChildState
	held          bool
}

func (s *childSession) close() {
	if s.held {
		_ = syscall.Flock(int(s.lock.Fd()), syscall.LOCK_UN)
	}
	if s.lock != nil {
		_ = s.lock.Close()
	}
	if s.dir != nil {
		_ = s.dir.Close()
	}
}
func (s *childSession) tryLock() error {
	if s.held {
		return nil
	}
	err := syscall.Flock(int(s.lock.Fd()), syscall.LOCK_EX|syscall.LOCK_NB)
	if err == nil {
		s.held = true
	}
	return err
}
func (s *childSession) reply(status int, code string, p *ChildPrepared) ChildReceipt {
	reply := childReply(status, code, p)
	if s.state != nil {
		reply.Phase = s.state.Phase
		rev := s.state.Revision
		reply.Revision = &rev
	}
	return reply
}
func (s *childSession) loadState() error {
	raw, err := childRead(s.dir, "state.json", ChildStateLimit)
	if err != nil {
		return err
	}
	state := &ChildState{}
	if err = ValidateChildControl("state", raw, state); err != nil {
		return err
	}
	if state.SessionID != s.descriptor.SessionID || state.ExecutionID != s.descriptor.ExecutionID || state.DescriptorDigest != childDigest(s.descriptorRaw) {
		return bad("identity_mismatch")
	}
	s.state = state
	return nil
}
func (s *childSession) save(point string) error {
	raw, err := childStateBytes(s.state)
	if err != nil {
		return err
	}
	return s.r.atomic(s.dir, "state.json", raw, false, point)
}
func (s *childSession) binding() error {
	d := s.descriptor
	runtime, err := childDirectory(d.RuntimePath, true)
	if err != nil {
		return err
	}
	info, err := runtime.Stat(".")
	runtime.Close()
	if err != nil || !childMatches(info, d.RuntimeDevice, d.RuntimeInode) {
		return bad("identity_mismatch")
	}
	path := filepath.Join(d.RuntimePath, d.SessionID)
	current, err := childDirectory(path, true)
	if err != nil {
		return err
	}
	info, err = current.Stat(".")
	current.Close()
	if err != nil || !childMatches(info, d.SessionDevice, d.SessionInode) {
		return bad("identity_mismatch")
	}
	info, err = s.dir.Lstat("writer.lock")
	actual, fdErr := s.lock.Stat()
	if err != nil || fdErr != nil || !childPrivate(info, false) || !childMatches(info, d.LockDevice, d.LockInode) || !os.SameFile(info, actual) {
		return bad("identity_mismatch")
	}
	raw, err := childRead(s.dir, "event-context.json", ChildDescriptorLimit)
	if err != nil || !bytes.Equal(raw, s.descriptorRaw) {
		return bad("identity_mismatch")
	}
	return nil
}
func (r *ChildRecorder) session(ref string) (*childSession, error) {
	runtime, id, path, err := ChildReference(ref)
	if err != nil {
		return nil, err
	}
	dir, err := childDirectory(path, true)
	if err != nil {
		return nil, err
	}
	s := &childSession{r: r, dir: dir}
	fail := func(err error) (*childSession, error) { s.close(); return nil, err }
	raw, err := childRead(dir, "event-context.json", ChildDescriptorLimit)
	if err != nil {
		return fail(bad("invalid_reference"))
	}
	if err = ValidateChildControl("descriptor", raw, &s.descriptor); err != nil {
		return fail(bad("invalid_reference"))
	}
	s.descriptorRaw = raw
	d := s.descriptor
	if d.SessionID != id || d.RuntimePath != runtime {
		return fail(bad("identity_mismatch"))
	}
	owner, err := r.owner(d.OwnerPID, false)
	if err != nil {
		return fail(err)
	}
	if owner.UID != d.UID || owner.Boot != d.BootID || owner.Namespace != d.PIDNamespace || owner.Start != d.OwnerStart {
		return fail(bad("identity_mismatch"))
	}
	s.lock, err = childFile(dir, "writer.lock", os.O_RDWR)
	if err != nil {
		return fail(bad("identity_mismatch"))
	}
	if err = s.binding(); err != nil {
		return fail(err)
	}
	return s, nil
}
func childBusy(err error) bool {
	return errors.Is(err, syscall.EWOULDBLOCK) || errors.Is(err, syscall.EAGAIN)
}
func childUnavailable(err error, p *ChildPrepared) ChildReceipt {
	code := Code(err)
	if code != "identity_mismatch" && code != "owner_gone" {
		code = "invalid_reference"
	}
	return childReply(26, code, p)
}

// disable is one publication attempt plus, at most, one nonblocking lock attempt.
func (s *childSession) disable(code string, p *ChildPrepared) ChildReceipt {
	gateErr := s.r.cut("disabled.publish")
	if gateErr == nil {
		gateErr = childPublishLatch(s.dir, "disabled")
	}
	persistent := gateErr == nil
	if !s.held {
		_ = s.tryLock()
	}
	if s.held {
		if s.loadState() == nil {
			// Never erase an uncertain intent. It is itself a durable refusal to append.
			if s.state.Phase == "writing" {
				persistent = true
			} else if s.state.Phase == "disabled" || s.state.Phase == "closed" {
				persistent = true
			} else {
				next := *s.state
				next.Phase = "disabled"
				next.Pending = nil
				if childAdvance(&next) == nil {
					s.state = &next
					if s.save("state-disable") == nil {
						persistent = true
					}
				}
			}
		}
	}
	reply := s.reply(23, code, p)
	reply.Phase = "disabled"
	switch {
	case persistent && s.held:
		reply.Stop = "quiescent"
	case gateErr == nil:
		reply.Stop = "requested"
	default:
		reply.Stop = "local"
		reply.Code = "disable_publish_failed"
	}
	return reply
}
func (s *childSession) stopped(p *ChildPrepared) (ChildReceipt, bool) {
	disabled, err := childLatch(s.dir, "disabled")
	if err != nil {
		return s.disable("control_unavailable", p), true
	}
	if disabled || s.state.Phase == "disabled" {
		return s.disable("session_disabled", p), true
	}
	return ChildReceipt{}, false
}
func (s *childSession) lockAndLoad(p *ChildPrepared) (ChildReceipt, bool) {
	if err := s.tryLock(); err != nil {
		if childBusy(err) {
			reply := s.reply(21, "writer_busy", p)
			if reply.Phase == "unavailable" {
				reply.Phase = "active"
			}
			return reply, false
		}
		return s.disable("control_unavailable", p), false
	}
	if err := s.binding(); err != nil {
		return childUnavailable(err, p), false
	}
	if err := s.loadState(); err != nil {
		if Code(err) == "identity_mismatch" {
			return childUnavailable(err, p), false
		}
		return s.disable("control_unavailable", p), false
	}
	if reply, stop := s.stopped(p); stop {
		return reply, false
	}
	if s.state.Phase == "writing" {
		return s.disable("stale_writing", p), false
	}
	return ChildReceipt{}, true
}
func (s *childSession) closing() (*ChildClosing, error) {
	raw, err := childRead(s.dir, "closing", ChildClosingLimit)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, err
	}
	closing := &ChildClosing{}
	if err = ValidateChildControl("closing", raw, closing); err != nil {
		return nil, err
	}
	if closing.SessionID != s.descriptor.SessionID {
		return nil, bad("identity_mismatch")
	}
	event, err := s.r.codec.Validate("event", closing.Record)
	if err != nil || event["execution_id"] != s.descriptor.ExecutionID {
		return nil, bad("identity_mismatch")
	}
	return closing, nil
}
func (s *childSession) root() (*Root, error) {
	root, err := OpenRoot(s.descriptor.RootPath)
	if err != nil {
		return nil, err
	}
	info, err := root.dir.Stat(".")
	if err != nil || !childMatches(info, s.descriptor.RootDevice, s.descriptor.RootInode) || root.Config() != s.descriptor.RootConfig {
		root.Close()
		return nil, bad("identity_mismatch")
	}
	return root, nil
}
func (s *childSession) ownerAuth(key string) error {
	digest, err := childKeyDigest(key)
	if err != nil {
		return err
	}
	if subtle.ConstantTimeCompare([]byte(digest), []byte(s.state.OwnerDigest)) != 1 {
		return bad("role_denied")
	}
	owner, err := s.r.owner(s.descriptor.OwnerPID, true)
	if err != nil {
		return bad("role_denied")
	}
	if owner.Start != s.descriptor.OwnerStart {
		return bad("role_denied")
	}
	return nil
}

// Open never returns a reference until started/Close/state/descriptor all succeed.
func (r *ChildRecorder) Open(rootPath, runtimePath string, ownerPID int, p *ChildPrepared) ChildReceipt {
	parsed, err := ParseChildPrepared(p.Bytes())
	if err != nil {
		return childReply(20, "invalid_value", nil)
	}
	p = parsed
	if p.Kind != "start" {
		return childReply(20, "role_denied", p)
	}
	if childAbsolute(rootPath, 1024) != nil || childAbsolute(runtimePath, 1024) != nil || childNested(rootPath, runtimePath) || childNested(runtimePath, rootPath) {
		return childReply(20, "invalid_value", p)
	}
	owner, err := r.owner(ownerPID, true)
	if err != nil {
		return childUnavailable(err, p)
	}
	runtime, err := childDirectory(runtimePath, true)
	if err != nil {
		return childUnavailable(err, p)
	}
	defer runtime.Close()
	root, err := OpenRoot(rootPath)
	if err != nil {
		return childReply(25, "installation_incompatible", p)
	}
	defer root.Close()
	rootInfo, err := root.dir.Stat(".")
	if err != nil {
		return childReply(25, "installation_incompatible", p)
	}
	if err = runtime.Mkdir(p.SessionID, 0700); err != nil {
		return childReply(23, "control_unavailable", p)
	}
	dir, err := runtime.OpenRoot(p.SessionID)
	if err != nil {
		return childReply(23, "control_unavailable", p)
	}
	s := &childSession{r: r, dir: dir}
	defer s.close()
	s.lock, err = childFile(dir, "writer.lock", os.O_CREATE|os.O_EXCL|os.O_RDWR)
	if err != nil {
		return childReply(23, "control_unavailable", p)
	}
	if err = s.tryLock(); err != nil {
		return childReply(23, "control_unavailable", p)
	}
	runtimeInfo, err := runtime.Stat(".")
	if err != nil {
		return s.disable("control_unavailable", p)
	}
	sessionInfo, err := dir.Stat(".")
	if err != nil || !childPrivate(sessionInfo, true) {
		return s.disable("control_unavailable", p)
	}
	lockInfo, err := s.lock.Stat()
	if err != nil {
		return s.disable("control_unavailable", p)
	}
	d := ChildDescriptor{Protocol: ChildProtocol, Version: 1, Interface: ChildInterface, Role: "event-only", SessionID: p.SessionID, ExecutionID: p.ExecutionID, EntryTime: p.Auxiliary, UID: owner.UID, BootID: owner.Boot, PIDNamespace: owner.Namespace, OwnerPID: owner.PID, OwnerStart: owner.Start, RuntimePath: runtimePath, RootPath: rootPath, RootConfig: root.Config()}
	d.RuntimeDevice, d.RuntimeInode = childFileID(runtimeInfo)
	d.SessionDevice, d.SessionInode = childFileID(sessionInfo)
	d.LockDevice, d.LockInode = childFileID(lockInfo)
	d.RootDevice, d.RootInode = childFileID(rootInfo)
	raw, err := json.Marshal(d)
	if err != nil || ValidateChildControl("descriptor", raw, nil) != nil {
		return s.disable("control_unavailable", p)
	}
	s.descriptor = d
	s.descriptorRaw = raw
	keyDigest, _ := childKeyDigest(p.OwnerKey)
	s.state = &ChildState{Protocol: ChildProtocol, Version: 1, SessionID: p.SessionID, ExecutionID: p.ExecutionID, DescriptorDigest: childDigest(raw), OwnerDigest: keyDigest, Phase: "creating"}
	if err = s.save("state-create"); err != nil {
		return s.disable("control_unavailable", p)
	}
	s.state.Phase = "writing"
	s.state.Pending = childPending(p)
	_ = childAdvance(s.state)
	if err = s.save("state-intent"); err != nil {
		return s.disable("indeterminate", p)
	}
	if err = r.cut("record.before-write"); err != nil {
		return s.disable("indeterminate", p)
	}
	w, err := root.Create(p.Record)
	if err != nil {
		return s.disable("indeterminate", p)
	}
	if r.writerHook != nil {
		r.writerHook(w)
	}
	info, statErr := w.file.Stat()
	closeErr := w.Close()
	cutErr := r.cut("record.after-close")
	if statErr != nil || closeErr != nil || cutErr != nil {
		return s.disable("indeterminate", p)
	}
	dev, ino := childFileID(info)
	s.state.RecordDevice = &dev
	s.state.RecordInode = &ino
	if reply, ok := s.finish(p, "committed", "ok"); !ok {
		return reply
	}
	if err = r.atomic(dir, "event-context.json", raw, true, "descriptor-publish"); err != nil {
		return s.disable("control_unavailable", p)
	}
	reply := s.reply(0, "ok", p)
	reply.Auxiliary = "sledra-ce1:" + filepath.Join(runtimePath, p.SessionID, "event-context.json")
	return reply
}
func (s *childSession) finish(p *ChildPrepared, result, code string) (ChildReceipt, bool) {
	next := *s.state
	next.Phase = "active"
	if p.Kind == "end" {
		next.Phase = "closed"
	}
	next.Pending = nil
	if result == "committed" {
		switch p.Kind {
		case "context":
			next.Context = bytes.Clone(p.Record)
		case "stage-start":
			e, _ := s.r.codec.Validate("event", p.Record)
			next.ActiveStage = &ChildStage{e["attributes"].(map[string]any)["stage"].(string), p.Auxiliary}
		case "stage-end":
			next.ActiveStage = nil
		case "end":
			next.End = bytes.Clone(p.Record)
		}
	}
	if err := childAdvance(&next); err != nil {
		return s.disable("indeterminate", p), false
	}
	next.Last = &ChildResult{p.RequestID, p.EventID, p.Digest, next.Revision, result, code, next.Phase}
	s.state = &next
	if err := s.save("state-commit"); err != nil {
		return s.disable("indeterminate", p), false
	}
	if disabled, err := childLatch(s.dir, "disabled"); err != nil || disabled {
		return s.disable("session_disabled", p), false
	}
	reply := s.reply(0, code, p)
	reply.Result = result
	if closing, err := s.closing(); err != nil {
		return s.disable("control_unavailable", p), false
	} else if closing != nil && next.Phase != "closed" {
		reply.Phase = "closing"
		if result == "noop" {
			reply.Status = 22
			reply.Result = "closed"
			reply.Code = "closing"
		}
	}
	return reply, true
}
func (r *ChildRecorder) Attach(ref string) ChildReceipt {
	s, err := r.session(ref)
	if err != nil {
		return childUnavailable(err, nil)
	}
	defer s.close()
	if reply, ok := s.lockAndLoad(nil); !ok {
		return reply
	}
	if s.state.Phase == "closed" {
		return s.reply(22, "closed", nil)
	}
	closing, err := s.closing()
	if err != nil {
		return s.disable("control_unavailable", nil)
	}
	if closing != nil {
		reply := s.reply(22, "closing", nil)
		reply.Phase = "closing"
		return reply
	}
	if s.state.Phase != "active" {
		return s.disable("control_unavailable", nil)
	}
	root, err := s.root()
	if err != nil {
		return s.disable("control_unavailable", nil)
	}
	defer root.Close()
	w, err := root.OpenWriter(s.descriptor.ExecutionID, s.descriptor.EntryTime)
	if err != nil {
		return s.disable("indeterminate", nil)
	}
	info, statErr := w.file.Stat()
	closeErr := w.Close()
	if statErr != nil || closeErr != nil || s.state.RecordInode == nil || !childMatches(info, *s.state.RecordDevice, *s.state.RecordInode) {
		return s.disable("indeterminate", nil)
	}
	reply := s.reply(0, "ok", nil)
	reply.Result = "attached"
	return reply
}
func (r *ChildRecorder) Disable(ref string) ChildReceipt {
	s, err := r.session(ref)
	if err != nil {
		return childUnavailable(err, nil)
	}
	defer s.close()
	return s.disable("session_disabled", nil)
}
func (r *ChildRecorder) Lookup(ref string, p *ChildPrepared) ChildReceipt {
	parsed, err := ParseChildPrepared(p.Bytes())
	if err != nil {
		return childReply(20, "invalid_value", nil)
	}
	p = parsed
	s, err := r.session(ref)
	if err != nil {
		return childUnavailable(err, p)
	}
	defer s.close()
	if p.ExecutionID != s.descriptor.ExecutionID {
		return childReply(20, "invalid_value", p)
	}
	unknown := func() ChildReceipt { reply := s.reply(24, "result_unknown", p); reply.Stop = "local"; return reply }
	if s.tryLock() != nil || s.binding() != nil || s.loadState() != nil {
		return unknown()
	}
	last := s.state.Last
	if s.state.Phase == "writing" || p.ExecutionID != s.state.ExecutionID || last == nil || last.RequestID != p.RequestID || last.EventID != p.EventID || last.Digest != p.Digest {
		return unknown()
	}
	reply := s.reply(0, "ok", p)
	reply.Result = "confirmed"
	revision := last.Revision
	reply.Revision = &revision
	if disabled, err := childLatch(s.dir, "disabled"); err != nil {
		return unknown()
	} else if disabled || s.state.Phase == "disabled" {
		reply.Phase = "disabled"
		reply.Stop = "quiescent"
	} else if closing, err := s.closing(); err != nil {
		return unknown()
	} else if closing != nil && reply.Phase != "closed" {
		reply.Phase = "closing"
	}
	return reply
}
