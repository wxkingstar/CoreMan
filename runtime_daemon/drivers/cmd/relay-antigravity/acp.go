package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"os"
	"os/exec"
	"strings"
	"sync"
	"time"

	"clawrelay-api/pkg/openai"
	"clawrelay-api/pkg/proc"
)

// The Antigravity CLI serves the Agent Client Protocol on stdin/stdout
// (`agy acp`): newline-delimited JSON-RPC 2.0. One process serves one turn
// here: initialize, session/new or session/resume, session/prompt, then exit.
// Everything that must hold for this turn only (MCP servers with their tokens,
// the model, the rules directory) travels in those requests, so nothing is
// written to the node's Antigravity configuration and no token reaches argv
// or disk.

// rpcMessage is any JSON-RPC message: a response (ID with Result or Error), a
// notification (Method) or a request from the agent (Method and ID).
type rpcMessage struct {
	ID     json.RawMessage `json:"id,omitempty"`
	Method string          `json:"method,omitempty"`
	Params json.RawMessage `json:"params,omitempty"`
	Result json.RawMessage `json:"result,omitempty"`
	Error  *rpcError       `json:"error,omitempty"`
}

type rpcError struct {
	Code    int    `json:"code"`
	Message string `json:"message"`
}

func (e *rpcError) Error() string { return e.Message }

// acpClient is one running `agy acp`.
type acpClient struct {
	cmd    *exec.Cmd
	stdin  io.WriteCloser
	writeM sync.Mutex
	nextID int

	// incoming carries every message from the agent in order and closes when
	// the process's stdout ends.
	incoming chan rpcMessage
	exited   chan struct{}
	waitErr  error
	stderr   *tailBuffer
}

// stderrTailBytes bounds how much trailing stderr a launch keeps.
const stderrTailBytes = 8 * 1024

type tailBuffer struct {
	mu    sync.Mutex
	limit int
	buf   []byte
}

func (t *tailBuffer) add(line string) {
	t.mu.Lock()
	defer t.mu.Unlock()
	t.buf = append(t.buf, line...)
	t.buf = append(t.buf, '\n')
	if over := len(t.buf) - t.limit; over > 0 {
		t.buf = append(t.buf[:0:0], t.buf[over:]...)
	}
}

func (t *tailBuffer) String() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	return string(t.buf)
}

// agyBinary is the CLI to run; tests point it at a fake.
var agyBinary = "agy"

// startACP launches `agy acp` in workingDir.
func startACP(args []string, workingDir string, envExtra map[string]string) (*acpClient, error) {
	cmd := exec.Command(agyBinary, append([]string{"acp"}, args...)...)
	proc.SetNewProcessGroup(cmd)
	cmd.Env = cleanEnv(envExtra)
	if workingDir != "" {
		cmd.Dir = workingDir
	}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, fmt.Errorf("failed to create stdin pipe: %v", err)
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, fmt.Errorf("failed to create stdout pipe: %v", err)
	}
	stderrPipe, err := cmd.StderrPipe()
	if err != nil {
		return nil, fmt.Errorf("failed to create stderr pipe: %v", err)
	}
	if err := proc.Start(cmd); err != nil {
		return nil, fmt.Errorf("failed to start agy: %v", err)
	}
	c := &acpClient{cmd: cmd, stdin: stdin, incoming: make(chan rpcMessage, 256), exited: make(chan struct{}), stderr: &tailBuffer{limit: stderrTailBytes}}

	var stderrDone sync.WaitGroup
	stderrDone.Add(1)
	go func() {
		defer stderrDone.Done()
		s := bufio.NewScanner(stderrPipe)
		s.Buffer(make([]byte, 0, 64*1024), 1024*1024)
		private := openai.PersonalPrivate(envExtra)
		for s.Scan() {
			line := openai.RedactCollaborationToken(s.Text(), envExtra)
			c.stderr.add(line)
			if private {
				line = openai.PrivateContentPreview(line, 0, true)
			}
			log.Printf("agy stderr: %s", line)
		}
		io.Copy(io.Discard, stderrPipe) //nolint:errcheck // backstop drain
	}()
	go func() {
		defer close(c.exited)
		s := bufio.NewScanner(stdout)
		s.Buffer(make([]byte, 0, 64*1024), 32*1024*1024)
		for s.Scan() {
			line := strings.TrimSpace(s.Text())
			if line == "" || !strings.HasPrefix(line, "{") {
				continue
			}
			var msg rpcMessage
			if err := json.Unmarshal([]byte(line), &msg); err != nil {
				log.Printf("[AGY PARSE ERR] %v", err)
				continue
			}
			c.incoming <- msg
		}
		if err := s.Err(); err != nil {
			log.Printf("WARNING: agy stdout scan aborted: %v (remaining output lost)", err)
			io.Copy(io.Discard, stdout) //nolint:errcheck // backstop drain
		}
		close(c.incoming)
		stderrDone.Wait()
		c.waitErr = proc.Wait(cmd)
	}()
	return c, nil
}

func (c *acpClient) write(msg map[string]any) error {
	data, err := json.Marshal(msg)
	if err != nil {
		return err
	}
	c.writeM.Lock()
	defer c.writeM.Unlock()
	_, err = c.stdin.Write(append(data, '\n'))
	return err
}

// send starts a request and returns its id.
func (c *acpClient) send(method string, params any) (string, error) {
	c.writeM.Lock()
	c.nextID++
	id := c.nextID
	c.writeM.Unlock()
	return fmt.Sprint(id), c.write(map[string]any{"jsonrpc": "2.0", "id": id, "method": method, "params": params})
}

func (c *acpClient) notify(method string, params any) error {
	return c.write(map[string]any{"jsonrpc": "2.0", "method": method, "params": params})
}

// reply answers a request the agent sent.
func (c *acpClient) reply(id json.RawMessage, result any) error {
	return c.write(map[string]any{"jsonrpc": "2.0", "id": id, "result": result})
}

func (c *acpClient) replyError(id json.RawMessage, code int, message string) error {
	return c.write(map[string]any{"jsonrpc": "2.0", "id": id, "error": map[string]any{"code": code, "message": message}})
}

// errExited is returned when the process ends before answering.
type errExited struct{ detail string }

func (e errExited) Error() string { return "agy exited before answering" + e.detail }

func (c *acpClient) exitError() error {
	<-c.exited
	detail := ""
	if line := lastLine(c.stderr.String()); line != "" {
		detail = ": " + line
	} else if c.waitErr != nil {
		detail = ": " + c.waitErr.Error()
	}
	return errExited{detail}
}

// call sends a request and waits for its response. Other messages that arrive
// meanwhile go to other (may be nil); requests from the agent are refused so
// the agent never waits on this client.
func (c *acpClient) call(ctx context.Context, method string, params any, timeout time.Duration, other func(rpcMessage)) (json.RawMessage, error) {
	id, err := c.send(method, params)
	if err != nil {
		return nil, c.exitOr(err)
	}
	timer := time.NewTimer(timeout)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-timer.C:
			return nil, fmt.Errorf("agy did not answer %s within %s", method, timeout)
		case msg, ok := <-c.incoming:
			if !ok {
				return nil, c.exitError()
			}
			if msg.Method == "" && idString(msg.ID) == id {
				if msg.Error != nil {
					return nil, msg.Error
				}
				return msg.Result, nil
			}
			if other != nil {
				other(msg)
			} else if msg.Method != "" && len(msg.ID) > 0 {
				_ = c.replyError(msg.ID, -32601, "not supported")
			}
		}
	}
}

// exitOr prefers the process's own failure over a broken pipe.
func (c *acpClient) exitOr(err error) error {
	select {
	case <-c.exited:
		return c.exitError()
	case <-time.After(500 * time.Millisecond):
		return err
	}
}

// close ends the process: closing stdin lets it finish cleanly, and the group
// is killed if it has not exited shortly after.
func (c *acpClient) close() {
	_ = c.stdin.Close()
	select {
	case <-c.exited:
	case <-time.After(3 * time.Second):
		proc.KillGroup(c.cmd)
		for range c.incoming {
		}
		<-c.exited
	}
}

func idString(raw json.RawMessage) string {
	return strings.Trim(strings.TrimSpace(string(raw)), `"`)
}

func lastLine(text string) string {
	lines := strings.Split(strings.TrimSpace(text), "\n")
	for i := len(lines) - 1; i >= 0; i-- {
		if line := strings.TrimSpace(lines[i]); line != "" {
			return openai.Truncate(line, 500)
		}
	}
	return ""
}

// cleanEnv builds the child environment: the driver's own minus task
// credentials left over from elsewhere, plus this turn's variables. Personal
// tools credentials reach only a turn that mounts their server. The bot cannot
// redirect the CLI's own configuration or account.
func cleanEnv(extra map[string]string) []string {
	var env []string
	for _, e := range os.Environ() {
		if strings.HasPrefix(e, "COREMAN_COLLABORATION_") || strings.HasPrefix(e, "COREMAN_BOT_HELP_") || strings.HasPrefix(e, "COREMAN_FEISHU_PERSONAL_") || strings.HasPrefix(e, "COREMAN_FEISHU_DOCS_") || strings.HasPrefix(e, "COREMAN_WECOM_PERSONAL_") || strings.HasPrefix(e, "COREMAN_SYSTEMS_MCP_") || strings.HasPrefix(e, "COREMAN_SYSTEM_MCP") {
			continue
		}
		env = append(env, e)
	}
	env = append(env, "AGY_CLI_DISABLE_AUTO_UPDATE=true")
	feishu := openai.FeishuPersonalEnabled(extra)
	wecom := openai.WecomPersonalEnabled(extra)
	docs := openai.FeishuDocsEnabled(extra)
	for k, v := range extra {
		if (strings.HasPrefix(k, "COREMAN_FEISHU_PERSONAL_") && !feishu) || (strings.HasPrefix(k, "COREMAN_WECOM_PERSONAL_") && !wecom) || (strings.HasPrefix(k, "COREMAN_FEISHU_DOCS_") && !docs) {
			continue
		}
		if reservedEnv(k) {
			continue
		}
		env = append(env, k+"="+v)
	}
	return env
}

// reservedEnv names variables that change which account, endpoint or
// configuration the CLI uses. GEMINI_API_KEY is left alone: the CLI ignores it
// unless the node's own settings select the API provider, and a bot's scripts
// may need it.
func reservedEnv(key string) bool {
	return strings.HasPrefix(key, "AGY_") || strings.HasPrefix(key, "ANTIGRAVITY_") || key == "GOOGLE_GEMINI_BASE_URL" || key == "GEMINI_DIR" || key == "HOME"
}
