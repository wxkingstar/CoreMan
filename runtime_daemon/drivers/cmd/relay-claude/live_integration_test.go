package main

import (
	"bufio"
	"bytes"
	"context"
	"crypto/rand"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"clawrelay-api/pkg/sessions"
)

// TestLiveRealClaude drives the relay over a Unix socket against the real
// `claude` CLI (logged in, on PATH). Opt in: RELAY_CLAUDE_LIVE_IT=1.
//
// It starts two background agents (alpha sleeps 40s then prints BG-OK, beta
// sleeps 300s), then checks: the first turn returns quickly with a background
// frame; a joined message is answered by the same process while the tasks
// run; stop_task stops beta only; alpha's completion arrives on attach as a
// turn containing BG-OK, followed by background_end finished.
func TestLiveRealClaude(t *testing.T) {
	if os.Getenv("RELAY_CLAUDE_LIVE_IT") != "1" {
		t.Skip("set RELAY_CLAUDE_LIVE_IT=1 to run against the real claude CLI")
	}
	model := os.Getenv("RELAY_CLAUDE_LIVE_IT_MODEL")
	if model == "" {
		model = "claude-haiku-5-5"
	}
	sockDir, err := os.MkdirTemp("", "rlit")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(sockDir)
	workDir := t.TempDir()
	sessionStore = sessions.New(t.TempDir())
	oldMeter := resumeMeter
	resumeMeter = newSessionMeter(t.TempDir())
	defer func() { resumeMeter = oldMeter }()

	mux := http.NewServeMux()
	mux.HandleFunc("/v1/chat/completions", chatCompletionsHandler)
	mux.HandleFunc("/live/attach", liveAttachHandler)
	mux.HandleFunc("/live/stop_task", liveStopTaskHandler)
	mux.HandleFunc("/live/close", liveCloseHandler)
	mux.HandleFunc("/live/list", liveListHandler)
	sock := filepath.Join(sockDir, "relay.sock")
	ln, err := net.Listen("unix", sock)
	if err != nil {
		t.Fatal(err)
	}
	srv := &http.Server{Handler: mux}
	go srv.Serve(ln)
	defer srv.Close()
	client := &http.Client{Transport: &http.Transport{DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", sock)
	}}}

	var u [16]byte
	rand.Read(u[:])
	u[6] = u[6]&0x0f | 0x40
	u[8] = u[8]&0x3f | 0x80
	sid := fmt.Sprintf("%x-%x-%x-%x-%x", u[0:4], u[4:6], u[6:8], u[8:10], u[10:])
	deadline := time.Now().Add(15 * time.Minute).Unix()
	defer func() {
		if s := lookupLive(sid); s != nil {
			s.close("reset")
			select {
			case <-s.done:
			case <-time.After(40 * time.Second):
			}
		}
	}()

	post := func(path string, body any) *http.Response {
		b, _ := json.Marshal(body)
		resp, err := client.Post("http://relay"+path, "application/json", bytes.NewReader(b))
		if err != nil {
			t.Fatal(err)
		}
		return resp
	}
	chat := func(text string, join bool) (string, time.Duration) {
		start := time.Now()
		body := map[string]any{
			"model": model, "stream": true, "stream_options": map[string]bool{"include_usage": true},
			"session_id": sid, "working_dir": workDir, "live_owner": "it-user", "live_deadline": deadline,
			"messages": []map[string]string{{"role": "user", "content": text}},
		}
		if join {
			body["live_join"] = true
		}
		resp := post("/v1/chat/completions", body)
		defer resp.Body.Close()
		var buf bytes.Buffer
		buf.ReadFrom(resp.Body)
		if resp.StatusCode != http.StatusOK {
			t.Fatalf("chat status %d: %s", resp.StatusCode, buf.String())
		}
		return buf.String(), time.Since(start)
	}

	first, took := chat("Launch exactly two background agents right now with the Agent tool, both with run_in_background: true. "+
		"Agent 1 must have the description \"task-alpha\" and this prompt: Run this command in the foreground with the Bash tool and report its output: "+
		"python3 -c 'import time; time.sleep(40); print(\"BG-OK\")'. "+
		"Agent 2 must have the description \"task-beta\" and this prompt: Run this command in the foreground with the Bash tool and report its output: "+
		"python3 -c 'import time; time.sleep(300); print(\"BG-B\")'. "+
		"After launching both, reply only with the word started and end your turn. Do not wait for them.", false)
	t.Logf("first turn took %s", took)
	if !strings.Contains(first, `"background":{`) {
		t.Fatalf("first turn has no background frame:\n%s", first)
	}
	if took > 35*time.Second {
		t.Fatalf("first turn took %s; it should not wait for the background tasks", took)
	}
	var betaID string
	for _, line := range strings.Split(first, "\n") {
		if !strings.Contains(line, `"background":{`) {
			continue
		}
		var frame struct {
			XRuntime struct {
				Background struct {
					Tasks []liveTask `json:"tasks"`
				} `json:"background"`
			} `json:"x_runtime"`
		}
		json.Unmarshal([]byte(strings.TrimPrefix(line, "data: ")), &frame)
		tasks := frame.XRuntime.Background.Tasks
		t.Logf("background tasks: %+v", tasks)
		for _, task := range tasks {
			if strings.Contains(strings.ToLower(task.Description), "beta") {
				betaID = task.ID
			}
		}
		if betaID == "" && len(tasks) == 2 {
			betaID = tasks[1].ID
		}
	}
	if betaID == "" {
		t.Fatalf("could not find the beta task:\n%s", first)
	}

	attachResp := post("/live/attach", map[string]string{"session_id": sid})
	if attachResp.StatusCode != http.StatusOK {
		t.Fatalf("attach status %d", attachResp.StatusCode)
	}
	defer attachResp.Body.Close()
	attachLines := make(chan string, 4096)
	go func() {
		defer close(attachLines)
		sc := bufio.NewScanner(attachResp.Body)
		sc.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
		for sc.Scan() {
			if l := sc.Text(); l != "" {
				attachLines <- l
			}
		}
	}()

	joined, took := chat("While the background agents run: reply with exactly the word PONG2 and nothing else.", true)
	t.Logf("joined turn took %s", took)
	if !strings.Contains(joined, `"live_joined"`) || !strings.Contains(joined, "PONG2") {
		t.Fatalf("joined turn:\n%s", joined)
	}

	resp := post("/live/stop_task", map[string]string{"session_id": sid, "task_id": betaID})
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("stop_task status %d", resp.StatusCode)
	}

	var all []string
	sawBGOK := false
	var end string
	timeout := time.After(4 * time.Minute)
read:
	for {
		select {
		case l, ok := <-attachLines:
			if !ok {
				break read
			}
			all = append(all, l)
			if strings.Contains(l, "BG-OK") {
				sawBGOK = true
			}
			if strings.Contains(l, `"background_end"`) {
				end = l
			}
		case <-timeout:
			t.Fatalf("attach did not end within 4 minutes:\n%s", strings.Join(all, "\n"))
		}
	}
	turnEnds := 0
	for _, l := range all {
		if strings.Contains(l, `"turn_end"`) {
			turnEnds++
		}
	}
	t.Logf("attach: %d lines, %d turn_end frames, end=%s", len(all), turnEnds, end)
	if !sawBGOK || turnEnds == 0 {
		t.Fatalf("attach never carried a turn with BG-OK:\n%s", strings.Join(all, "\n"))
	}
	if !strings.Contains(end, `"state":"finished"`) || !strings.Contains(end, `"status":"stopped"`) || !strings.Contains(end, `"status":"completed"`) {
		t.Fatalf("background_end = %s", end)
	}
	if s := lookupLive(sid); s != nil && !s.isEnded() {
		t.Fatal("session still live after background_end")
	}
}
