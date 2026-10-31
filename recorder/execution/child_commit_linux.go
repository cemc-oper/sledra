//go:build linux

package execution

import (
	"bytes"
	"encoding/json"
	"errors"
	"math"
	"os"
)

func (r *ChildRecorder) Commit(ref, role, key string, p *ChildPrepared) ChildReceipt {
	parsed, err := ParseChildPrepared(p.Bytes())
	if err != nil {
		return childReply(20, childInputCode(err), nil)
	}
	p = parsed
	if role != "owner" && role != "event" || p.Kind == "start" || role == "event" && p.Kind != "event" {
		return childReply(20, "role_denied", p)
	}
	s, err := r.session(ref)
	if err != nil {
		return childUnavailable(err, p)
	}
	defer s.close()
	// This immutable-key snapshot is only for authorization before closing publication.
	// Mutable lifecycle/admission decisions are always re-read under writer.lock.
	if err = s.loadState(); err != nil {
		if Code(err) == "identity_mismatch" {
			return childUnavailable(err, p)
		}
		return s.disable("control_unavailable", p)
	}
	if p.ExecutionID != s.descriptor.ExecutionID {
		return childReply(20, "invalid_value", p)
	}
	if role == "owner" {
		if err = s.ownerAuth(key); err != nil {
			return s.reply(20, "role_denied", p)
		}
	}
	if reply, stop := s.stopped(p); stop {
		return reply
	}
	if p.Kind == "end" {
		closing, err := s.closing()
		if err != nil {
			return s.disable("control_unavailable", p)
		}
		if closing == nil {
			event, err := r.codec.Validate("event", p.Record)
			if err != nil || event["name"] != "execution.ended" {
				return s.reply(20, "invalid_value", p)
			}
			claim := ChildClosing{ChildProtocol, 1, s.descriptor.SessionID, p.RequestID, p.EventID, p.Digest, bytes.Clone(p.Record)}
			raw, err := json.Marshal(claim)
			if err != nil || ValidateChildControl("closing", raw, nil) != nil {
				return s.reply(20, "invalid_value", p)
			}
			err = r.atomic(s.dir, "closing", raw, true, "closing-publish")
			if err != nil && !errors.Is(err, os.ErrExist) {
				return s.disable("control_unavailable", p)
			}
			closing, err = s.closing()
			if err != nil || closing == nil {
				return s.disable("control_unavailable", p)
			}
		}
		same, err := r.sameAttributes(closing.Record, p.Record)
		if err != nil {
			return s.disable("control_unavailable", p)
		}
		if !same {
			reply := s.reply(22, "conflicting_end", p)
			reply.Phase = "closing"
			if s.state.Phase == "closed" {
				reply.Phase = "closed"
			}
			return reply
		}
		// A subsequent explicit same-end after Busy uses the complete first claim.
		p = &ChildPrepared{RequestID: closing.RequestID, ExecutionID: s.descriptor.ExecutionID, EventID: closing.EventID, Auxiliary: "-", SessionID: "-", OwnerKey: "-", Digest: closing.Digest, Kind: "end", Record: bytes.Clone(closing.Record)}
	}
	if reply, ok := s.lockAndLoad(p); !ok {
		if reply.Status == 21 && p.Kind == "end" {
			reply.Phase = "closing"
		}
		return reply
	}
	if role == "owner" {
		if err = s.ownerAuth(key); err != nil {
			return s.reply(20, "role_denied", p)
		}
	}
	if s.state.Phase == "closed" {
		if p.Kind == "end" {
			same, err := r.sameAttributes(s.state.End, p.Record)
			if err != nil {
				return s.disable("control_unavailable", p)
			}
			if same {
				reply := s.reply(0, "same_end", p)
				reply.Result = "noop"
				return reply
			}
			return s.reply(22, "conflicting_end", p)
		}
		return s.reply(22, "closed", p)
	}
	if s.state.Phase != "active" {
		return s.disable("control_unavailable", p)
	}
	if p.Kind == "context" && s.state.Context != nil {
		same, err := r.sameAttributes(s.state.Context, p.Record)
		if err != nil {
			return s.disable("control_unavailable", p)
		}
		if !same {
			return s.disable("conflicting_context", p)
		}
		reply, _ := s.finish(p, "noop", "same_context")
		return reply
	}
	event, _ := r.codec.Validate("event", p.Record)
	attrs := event["attributes"].(map[string]any)
	if p.Kind == "stage-start" && s.state.ActiveStage != nil || p.Kind == "stage-end" && (s.state.ActiveStage == nil || s.state.ActiveStage.ID != attrs["stage_id"] || s.state.ActiveStage.Name != attrs["stage"]) {
		return s.reply(20, "stage_order", p)
	}
	if s.state.Revision >= math.MaxInt64-1 {
		return s.disable("control_unavailable", p)
	}
	// Two monotonic gate reads define one admission group (P05). No retry loop.
	if err = r.cut("admission.before-gates"); err != nil {
		return s.disable("control_unavailable", p)
	}
	if reply, stop := s.stopped(p); stop {
		return reply
	}
	if err = r.cut("admission.after-disabled"); err != nil {
		return s.disable("control_unavailable", p)
	}
	closing, err := s.closing()
	if err != nil {
		return s.disable("control_unavailable", p)
	}
	if closing != nil && p.Kind != "end" {
		reply := s.reply(22, "closing", p)
		reply.Phase = "closing"
		return reply
	}
	if p.Kind == "end" && (closing == nil || closing.RequestID != p.RequestID) {
		return s.disable("control_unavailable", p)
	}
	if err = r.cut("admission.accepted"); err != nil {
		return s.disable("control_unavailable", p)
	}
	root, err := s.root()
	if err != nil {
		return s.disable("control_unavailable", p)
	}
	defer root.Close()
	next := *s.state
	next.Phase = "writing"
	next.Pending = childPending(p)
	_ = childAdvance(&next)
	s.state = &next
	if err = s.save("state-intent"); err != nil {
		return s.disable("indeterminate", p)
	}
	if reply, stop := s.stopped(p); stop {
		return reply
	}
	if err = r.cut("record.before-open"); err != nil {
		return s.disable("indeterminate", p)
	}
	w, err := root.OpenWriter(p.ExecutionID, s.descriptor.EntryTime)
	if err != nil {
		return s.disable("indeterminate", p)
	}
	info, statErr := w.file.Stat()
	if statErr != nil || s.state.RecordInode == nil || !childMatches(info, *s.state.RecordDevice, *s.state.RecordInode) {
		_ = w.Close()
		return s.disable("indeterminate", p)
	}
	if err = r.cut("record.before-write"); err != nil {
		_ = w.Close()
		return s.disable("indeterminate", p)
	}
	writeErr := w.Append(p.Record)
	afterErr := r.cut("record.after-write")
	closeErr := w.Close()
	closeCut := r.cut("record.after-close")
	if writeErr != nil || afterErr != nil || closeErr != nil || closeCut != nil {
		return s.disable("indeterminate", p)
	}
	reply, _ := s.finish(p, "committed", "ok")
	return reply
}
func (r *ChildRecorder) sameAttributes(a, b []byte) (bool, error) {
	left, err := r.codec.Validate("event", a)
	if err != nil {
		return false, err
	}
	right, err := r.codec.Validate("event", b)
	if err != nil {
		return false, err
	}
	left["event_id"] = right["event_id"]
	left["occurred_at"] = right["occurred_at"]
	return Equivalent("event", left, right), nil
}
func childInputCode(err error) string {
	switch Code(err) {
	case "duplicate_key":
		return "duplicate_key"
	case "limit_exceeded":
		return "limit_exceeded"
	case "reserved_event_name":
		return "reserved_event_name"
	case "role_denied":
		return "role_denied"
	default:
		return "invalid_value"
	}
}
