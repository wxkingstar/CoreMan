package main

import (
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"os/exec"
	"strings"
	"sync"
	"sync/atomic"
)

// claudeProc is one running `claude` process. A process launched with a
// writable stdin (keepStdin) stays able to take more messages and control
// requests after its first turn: CLI 2.1.318 then returns a turn's result as
// soon as the main turn ends, keeps background tasks running past the print
// mode wait ceiling, and continues on its own with one more turn when a
// background task finishes. stdin is closed at the first result that leaves
// no background task running, which makes the CLI exit like the
// close-after-first-message processes always did.
type claudeProc struct {
	cmd   *exec.Cmd
	stdin io.WriteCloser // nil when stdin ended after the first message
	tasks *bgTracker

	writeMu sync.Mutex // serializes whole lines on stdin

	mu          sync.Mutex
	stdinClosed bool
	// closing makes the next result close stdin whatever runs (interrupt).
	closing bool
	// decisions holds, per result line not yet taken by the consumer,
	// whether stdin stayed open after it (the process went on living).
	decisions []bool
	// exitedOpen: the process exited while stdin was still open (a crash,
	// not the end we asked for).
	exitedOpen bool
	exitErr    error
	exited     chan struct{}
}

var errStdinClosed = errors.New("claude stdin is closed")

var controlSeq atomic.Int64

// writeLine writes one stream-json line to stdin.
func (p *claudeProc) writeLine(b []byte) error {
	if p.stdin == nil {
		return errStdinClosed
	}
	p.mu.Lock()
	closed := p.stdinClosed
	p.mu.Unlock()
	if closed {
		return errStdinClosed
	}
	if len(b) == 0 || b[len(b)-1] != '\n' {
		b = append(append([]byte(nil), b...), '\n')
	}
	p.writeMu.Lock()
	defer p.writeMu.Unlock()
	_, err := p.stdin.Write(b)
	return err
}

// control sends a control_request; its control_response is consumed by
// observe and never forwarded.
func (p *claudeProc) control(request map[string]any) error {
	id := fmt.Sprintf("relay-%d", controlSeq.Add(1))
	line, _ := json.Marshal(map[string]any{"type": "control_request", "request_id": id, "request": request})
	log.Printf("[live] control_request %s subtype=%v", id, request["subtype"])
	return p.writeLine(line)
}

func (p *claudeProc) stopTask(taskID string) error {
	return p.control(map[string]any{"subtype": "stop_task", "task_id": taskID})
}

// interrupt stops the running turn and, per CLI 2.1.318, every background
// task with it; the result that follows closes stdin so the process exits.
func (p *claudeProc) interrupt() error {
	p.mu.Lock()
	p.closing = true
	p.mu.Unlock()
	return p.control(map[string]any{"subtype": "interrupt"})
}

// closeStdin ends the input; with no background task left the CLI exits.
func (p *claudeProc) closeStdin() {
	p.mu.Lock()
	defer p.mu.Unlock()
	p.closeStdinLocked()
}

func (p *claudeProc) closeStdinLocked() {
	if p.stdin == nil || p.stdinClosed {
		return
	}
	p.stdinClosed = true
	p.closing = true
	if err := p.stdin.Close(); err != nil {
		log.Printf("[live] closing claude stdin: %v", err)
	}
}

// stdinOpen reports whether the process still takes messages.
func (p *claudeProc) stdinOpen() bool {
	p.mu.Lock()
	defer p.mu.Unlock()
	return p.stdin != nil && !p.stdinClosed && !p.closing
}

// observe sees every stdout line of a keepStdin process before it is
// forwarded, in order. It keeps the background task table, decides at each
// result whether stdin stays open, and swallows control responses. It returns
// false for lines that must not be forwarded.
func (p *claudeProc) observe(line string) bool {
	if strings.HasPrefix(line, `{"type":"stream_event"`) {
		return true
	}
	var head struct {
		Type     string `json:"type"`
		Subtype  string `json:"subtype"`
		Response struct {
			Subtype   string `json:"subtype"`
			RequestID string `json:"request_id"`
			Error     string `json:"error"`
		} `json:"response"`
		RequestID string `json:"request_id"`
		Request   struct {
			Subtype string `json:"subtype"`
		} `json:"request"`
	}
	if json.Unmarshal([]byte(line), &head) != nil {
		return true
	}
	switch head.Type {
	case "control_response":
		log.Printf("[live] control_response %s %s %s", head.Response.RequestID, head.Response.Subtype, head.Response.Error)
		return false
	case "control_request":
		// With stdin open the CLI waits for our answer, so every request is
		// answered at once: a permission prompt (can_use_tool) is denied —
		// the tool gets an error result and the turn goes on (verified on
		// 2.1.318) — and anything else is refused.
		log.Printf("[live] claude sent control_request %s subtype=%s; refusing", head.RequestID, head.Request.Subtype)
		reply := controlRefusal(head.RequestID, head.Request.Subtype)
		go func() {
			if err := p.writeLine(reply); err != nil {
				log.Printf("[live] answering control_request %s: %v", head.RequestID, err)
			}
		}()
		return false
	case "system":
		if isTaskEventSubtype(head.Subtype) {
			p.tasks.observe([]byte(line))
		}
	case "result":
		p.mu.Lock()
		if !p.stdinClosed && (p.closing || len(p.tasks.running()) == 0) {
			p.closeStdinLocked()
		}
		p.decisions = append(p.decisions, !p.stdinClosed)
		p.mu.Unlock()
	}
	return true
}

// controlRefusal answers a control_request the CLI sent us.
func controlRefusal(requestID, subtype string) []byte {
	response := map[string]any{"subtype": "error", "request_id": requestID, "error": "not supported by relay"}
	if subtype == "can_use_tool" {
		response = map[string]any{"subtype": "success", "request_id": requestID,
			"response": map[string]any{"behavior": "deny", "message": "Permission prompts are not supported by this relay; the tool call was denied."}}
	}
	line, _ := json.Marshal(map[string]any{"type": "control_response", "response": response})
	return line
}

// takeResult returns, for the next result line the consumer reached, whether
// the process stays alive after it (background phase).
func (p *claudeProc) takeResult() bool {
	if p == nil || p.stdin == nil {
		return false
	}
	p.mu.Lock()
	defer p.mu.Unlock()
	if len(p.decisions) == 0 {
		return !p.stdinClosed
	}
	d := p.decisions[0]
	p.decisions = p.decisions[1:]
	return d
}

func (p *claudeProc) markExited(err error) {
	p.mu.Lock()
	p.exitedOpen = p.stdin != nil && !p.stdinClosed
	p.stdinClosed = true
	p.exitErr = err
	p.mu.Unlock()
	close(p.exited)
}

// lineHead reads a stream-json line's type and subtype.
func lineHead(line string) (typ, subtype string) {
	if strings.HasPrefix(line, `{"type":"stream_event"`) {
		return "stream_event", ""
	}
	var head struct {
		Type    string `json:"type"`
		Subtype string `json:"subtype"`
	}
	_ = json.Unmarshal([]byte(line), &head)
	return head.Type, head.Subtype
}
