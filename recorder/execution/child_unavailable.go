//go:build !linux

package execution

// Preserve compilation of legacy paths on other hosts; CE-1 is Linux only.
type ChildRecorder struct{}

func ChildSupported() bool                      { return false }
func NewChildRecorder() (*ChildRecorder, error) { return nil, bad("installation_incompatible") }
func childUnsupported(p *ChildPrepared) ChildReceipt {
	return childReply(25, "installation_incompatible", p)
}
func (r *ChildRecorder) Open(root, runtime string, pid int, p *ChildPrepared) ChildReceipt {
	return childUnsupported(p)
}
func (r *ChildRecorder) Commit(ref, role, key string, p *ChildPrepared) ChildReceipt {
	return childUnsupported(p)
}
func (r *ChildRecorder) Attach(ref string) ChildReceipt                   { return childUnsupported(nil) }
func (r *ChildRecorder) Lookup(ref string, p *ChildPrepared) ChildReceipt { return childUnsupported(p) }
func (r *ChildRecorder) Disable(ref string) ChildReceipt                  { return childUnsupported(nil) }

func (r *ChildRecorder) Bind(ref string) (ChildReceipt, string) { return childUnsupported(nil), "" }
func (r *ChildRecorder) ClaimDiagnostic(ref string) (bool, error) {
	return false, bad("installation_incompatible")
}
