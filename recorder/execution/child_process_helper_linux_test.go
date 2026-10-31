//go:build linux

package execution

// This helper exists only in `go test -c` output. The production command has no
// corresponding flags/environment reader. Bash callers exercise the real codec,
// coordinator, procfs identity, files, flock and writer in independent processes.
import (
	"bytes"
	"encoding/json"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"strconv"
	"strings"
	"testing"
	"time"
)

type ce04Process struct {
	action      string
	prepared    *ChildPrepared
	selected    bool
	pauseAction string
	faults      map[string]bool
}

func (h *ce04Process) trace(point string, extra map[string]any) {
	row := map[string]any{"point": point, "pid": os.Getpid(), "actor": os.Getenv("CE04_ACTOR"), "command": h.action, "time_ns": time.Now().UnixNano()}
	if p := h.prepared; p != nil {
		row["request_id"], row["event_id"], row["execution_id"], row["kind"], row["digest"] = p.RequestID, p.EventID, p.ExecutionID, p.Kind, p.Digest
	}
	for k, v := range extra {
		row[k] = v
	}
	raw, err := json.Marshal(row)
	if err != nil {
		panic(err)
	}
	f, err := os.OpenFile(os.Getenv("CE04_TRACE"), os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0600)
	if err != nil {
		panic(err)
	}
	n, err := f.Write(append(raw, '\n'))
	closeErr := f.Close()
	if err != nil || n != len(raw)+1 || closeErr != nil {
		panic("test trace unavailable")
	}
}
func (h *ce04Process) hit(point string) error {
	h.trace(point, nil)
	if !h.selected {
		return nil
	}
	if strings.Contains(","+os.Getenv("CE04_PAUSE")+",", ","+point+",") && h.action == h.pauseAction {
		c, err := net.DialTimeout("unix", os.Getenv("CE04_SOCKET"), 10*time.Second)
		if err != nil {
			panic(err)
		}
		_ = c.SetDeadline(time.Now().Add(20 * time.Second))
		if err = json.NewEncoder(c).Encode(map[string]any{"pid": os.Getpid(), "point": point}); err != nil {
			panic(err)
		}
		var release [1]byte
		if _, err = io.ReadFull(c, release[:]); err != nil {
			panic(err)
		}
		_ = c.Close()
	}
	if h.faults[point] {
		delete(h.faults, point)
		h.trace("fault", map[string]any{"at": point})
		return io.ErrUnexpectedEOF
	}
	return nil
}
func (h *ce04Process) install(r *ChildRecorder) {
	r.fault = h.hit
	r.writerHook = func(w *Writer) {
		write, closeFile := w.write, w.closeFile
		w.write = func(raw []byte) (int, error) {
			if err := h.hit("write.enter"); err != nil {
				return 0, err
			}
			// Each injected short/error write still uses the actual JSONL file descriptor.
			data := raw
			mode := ""
			if h.selected {
				mode = os.Getenv("CE04_IO_FAULT")
			}
			if mode == "short" {
				data = raw[:5]
			}
			var n int
			var err error
			if mode == "write-error" {
				err = io.ErrUnexpectedEOF
			} else {
				h.trace("write.syscall", nil)
				n, err = write(data)
			}
			if mode == "full-error" {
				err = io.ErrUnexpectedEOF
			}
			h.trace("write.return", map[string]any{"requested": len(raw), "written": n, "error": err != nil, "record": string(raw)})
			if cutErr := h.hit("write.after-return"); cutErr != nil {
				err = cutErr
			}
			return n, err
		}
		w.closeFile = func() error {
			_ = h.hit("close.enter")
			err := closeFile()
			if h.selected && os.Getenv("CE04_IO_FAULT") == "close" {
				err = io.ErrUnexpectedEOF
			}
			h.trace("close.return", map[string]any{"error": err != nil})
			if cutErr := h.hit("close.after-return"); cutErr != nil {
				err = cutErr
			}
			return err
		}
	}
}
func ce04Options(args []string) map[string]string {
	opts := map[string]string{}
	for i := 0; i+1 < len(args); i += 2 {
		opts[args[i]] = args[i+1]
	}
	return opts
}
func ce04Delegate(h *ce04Process, args []string) {
	cmd := exec.Command(os.Getenv("CE04_REAL_BIN"), args...)
	cmd.Stdin = os.Stdin
	cmd.Stderr = os.Stderr
	var output bytes.Buffer
	cmd.Stdout = &output
	err := cmd.Run()
	status := 0
	if err != nil {
		if exit, ok := err.(*exec.ExitError); ok {
			status = exit.ExitCode()
		} else {
			status = 99
		}
	}
	if h.action == "prepare" && status == 0 {
		p, err := ParseChildPrepared(output.Bytes())
		if err != nil {
			panic(err)
		}
		h.prepared = p
		// Save complete event bytes, never the start frame's owner key.
		h.trace("prepared", map[string]any{"record": string(p.Record) + "\n"})
	}
	h.trace("delegated", map[string]any{"status": status})
	_, _ = os.Stdout.Write(output.Bytes())
	os.Exit(status)
}
func TestCE04ProcessHelper(t *testing.T) {
	if os.Getenv("CE04_HELPER") != "1" {
		return
	}
	index := 0
	for index < len(os.Args) && os.Args[index] != "--" {
		index++
	}
	args := os.Args[index+1:]
	h := &ce04Process{faults: map[string]bool{}}
	if len(args) < 3 || args[0] != "execution" || args[1] != "child-events" {
		h.action = "legacy"
		ce04Delegate(h, args)
		return
	}
	h.action = args[2]
	opts := ce04Options(args[3:])
	switch h.action {
	case "open", "commit", "result":
		var err error
		h.prepared, err = ReadChildPrepared(os.Stdin)
		if err != nil {
			panic(err)
		}
	}
	h.trace("call", nil)
	switch h.action {
	case "open", "commit", "result", "disable":
	default:
		ce04Delegate(h, args)
		return
	}
	match := os.Getenv("CE04_COMMAND")
	if match == "" {
		match = "commit"
	}
	h.pauseAction = strings.Split(match, ",")[0]
	h.selected = strings.Contains(","+match+",", ","+h.action+",") && (os.Getenv("CE04_KIND") == "" || h.prepared != nil && h.prepared.Kind == os.Getenv("CE04_KIND"))
	for _, point := range strings.Split(os.Getenv("CE04_FAULTS"), ",") {
		if point != "" {
			h.faults[point] = true
		}
	}
	r, err := NewChildRecorder()
	if err != nil {
		panic(err)
	}
	h.install(r)
	var reply ChildReceipt
	switch h.action {
	case "open":
		pid, err := strconv.Atoi(opts["--owner-pid"])
		if err != nil {
			panic(err)
		}
		reply = r.Open(opts["--root"], opts["--runtime-dir"], pid, h.prepared)
	case "commit":
		reply = r.Commit(opts["--context"], opts["--role"], opts["--owner-key"], h.prepared)
	case "result":
		p := h.prepared
		if opts["--request-id"] != p.RequestID || opts["--event-id"] != p.EventID || opts["--record-sha256"] != p.Digest {
			panic("lookup identity mismatch")
		}
		reply = r.Lookup(opts["--context"], p)
	case "disable":
		reply = r.Disable(opts["--context"])
	}
	// Release of the real writer lock precedes this receipt delivery barrier.
	h.trace("receipt", map[string]any{"status": reply.Status, "result": reply.Result, "code": reply.Code, "phase": reply.Phase, "stop": reply.Stop, "wire": string(reply.Bytes())})
	_ = h.hit("receipt.before-delivery")
	if h.selected && os.Getenv("CE04_DROP_RECEIPT") == "1" {
		os.Exit(99)
	}
	if h.selected && os.Getenv("CE04_BAD_RECEIPT") == "1" {
		fmt.Print("corrupt\n")
		os.Exit(0)
	}
	_, _ = os.Stdout.Write(reply.Bytes())
	os.Exit(reply.Status)
}
