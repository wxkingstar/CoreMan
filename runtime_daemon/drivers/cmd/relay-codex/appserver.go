package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"os/exec"

	"clawrelay-api/pkg/proc"
)

type rpcMessage struct {
	ID     *int            `json:"id,omitempty"`
	Method string          `json:"method,omitempty"`
	Params json.RawMessage `json:"params,omitempty"`
	Result json.RawMessage `json:"result,omitempty"`
	Error  json.RawMessage `json:"error,omitempty"`
}

// appServer is one `codex app-server` child spoken to over stdio JSON-RPC,
// already initialized (with experimentalApi). Calls and notifications end
// when the child exits or ctx ends.
type appServer struct {
	ctx      context.Context
	cmd      *exec.Cmd
	stdin    io.WriteCloser
	messages chan rpcMessage
	stderr   *tailBuffer
	nextID   int
}

func startAppServer(ctx context.Context, workingDir string) (*appServer, error) {
	cmd := exec.Command("codex", "app-server")
	proc.SetNewProcessGroup(cmd)
	cmd.Env = cleanEnv(nil)
	if workingDir != "" {
		cmd.Dir = workingDir
	}
	stdin, err := cmd.StdinPipe()
	if err != nil {
		return nil, err
	}
	stdout, err := cmd.StdoutPipe()
	if err != nil {
		return nil, err
	}
	srv := &appServer{ctx: ctx, cmd: cmd, stdin: stdin, messages: make(chan rpcMessage, 64), stderr: &tailBuffer{limit: stderrTailBytes}}
	cmd.Stderr = lineWriter{srv.stderr}
	if err := proc.Start(cmd); err != nil {
		return nil, fmt.Errorf("failed to start codex app-server: %v", err)
	}
	go func() {
		defer close(srv.messages)
		s := bufio.NewScanner(stdout)
		s.Buffer(make([]byte, 0, 64*1024), 64*1024*1024)
		for s.Scan() {
			var msg rpcMessage
			if json.Unmarshal(s.Bytes(), &msg) == nil {
				srv.messages <- msg
			}
		}
		io.Copy(io.Discard, stdout) //nolint:errcheck // keep the child from blocking on a full pipe
	}()
	if _, err := srv.call("initialize", map[string]any{
		"clientInfo":   map[string]string{"name": "coreman-relay-codex", "version": version},
		"capabilities": map[string]bool{"experimentalApi": true},
	}); err != nil {
		srv.close()
		return nil, err
	}
	if _, err := stdin.Write([]byte(`{"method":"initialized"}` + "\n")); err != nil {
		srv.close()
		return nil, err
	}
	return srv, nil
}

func (a *appServer) close() {
	a.stdin.Close()
	proc.KillGroup(a.cmd)
	_ = proc.Wait(a.cmd)
}

// next returns the next message, or an error once codex exits or ctx ends.
func (a *appServer) next() (rpcMessage, error) {
	select {
	case msg, ok := <-a.messages:
		if !ok {
			return msg, fmt.Errorf("codex app-server exited: %s", lastLine(a.stderr.String()))
		}
		return msg, nil
	case <-a.ctx.Done():
		return rpcMessage{}, a.ctx.Err()
	}
}

// call sends one request and returns its result, skipping notifications.
func (a *appServer) call(method string, params any) (json.RawMessage, error) {
	a.nextID++
	id := a.nextID
	raw, _ := json.Marshal(params)
	line, _ := json.Marshal(rpcMessage{ID: &id, Method: method, Params: raw})
	if _, err := a.stdin.Write(append(line, '\n')); err != nil {
		return nil, err
	}
	for {
		msg, err := a.next()
		if err != nil {
			return nil, err
		}
		if msg.ID == nil || *msg.ID != id || msg.Method != "" {
			continue
		}
		if len(msg.Error) > 0 {
			return nil, fmt.Errorf("%s: %s", method, msg.Error)
		}
		return msg.Result, nil
	}
}
