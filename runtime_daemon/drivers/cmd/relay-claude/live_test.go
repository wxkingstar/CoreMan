package main

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"clawrelay-api/pkg/sessions"
)

// fakeLiveClaude acts out the stream-json protocol of a claude process with
// a writable stdin (CLI 2.1.318 shapes): a message containing "spawn" starts
// background tasks t1 and t2 (t1 with a nested task c1); a file
// complete_<id> in $FAKE_DIR finishes a task and continues with a turn; a
// file "crash" makes it exit abruptly; stop_task / interrupt control
// requests behave like the CLI; a message containing "hang" waits for an
// interrupt; $FAKE_ABSORB seconds lets a completion turn absorb a message
// written while it runs. Every stdin line is logged to $FAKE_DIR/stdin.log.
// modelUsage is cumulative over the process, usage is per turn.
const fakeLiveClaude = `#!/usr/bin/env python3
import json, os, sys, threading, queue, time
D = os.environ["FAKE_DIR"]
open(os.path.join(D, "launches"), "a").write("x\n")
open(os.path.join(D, "ceiling"), "w").write(os.environ.get("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", ""))
def out(o):
    sys.stdout.write(json.dumps(o) + "\n"); sys.stdout.flush()
q = queue.Queue()
def reader():
    for line in sys.stdin:
        with open(os.path.join(D, "stdin.log"), "a") as f:
            f.write(line)
        q.put(line)
    q.put(None)
threading.Thread(target=reader, daemon=True).start()
tasks = {}
cum = [0]
eof = [False]
def text(s):
    out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": s}}})
def result(subtype="success", err=False):
    cum[0] += 10
    out({"type": "result", "subtype": subtype, "is_error": err, "result": "",
         "usage": {"input_tokens": 1, "output_tokens": 10},
         "modelUsage": {"m": {"inputTokens": cum[0] * 3, "outputTokens": cum[0], "cacheReadInputTokens": 0, "cacheCreationInputTokens": 0, "costUSD": cum[0] / 1000}},
         "total_cost_usd": cum[0] / 1000})
def bg_changed():
    out({"type": "system", "subtype": "background_tasks_changed",
         "tasks": [{"task_id": k, "task_type": "local_agent", "description": "task " + k} for k, v in tasks.items() if v == "running"]})
def end_task(tid, status):
    tasks[tid] = status
    out({"type": "system", "subtype": "task_updated", "task_id": tid, "patch": {"status": "killed" if status == "stopped" else status, "end_time": int(time.time() * 1000)}})
    out({"type": "system", "subtype": "task_notification", "task_id": tid, "status": status})
    bg_changed()
def msg_text(m):
    c = m["message"]["content"]
    if isinstance(c, str):
        return c
    return "".join(b.get("text", "") for b in c if b.get("type") == "text")
def interrupt(m):
    out({"type": "control_response", "response": {"subtype": "success", "request_id": m["request_id"], "response": {}}})
    for k, v in list(tasks.items()):
        if v == "running":
            end_task(k, "stopped")
    result("error_during_execution", True)
def turn(t, absorb=0.0):
    out({"type": "system", "subtype": "init"})
    if "spawn" in t:
        for tid in ("t1", "t2"):
            tasks[tid] = "running"
            bg_changed()
            out({"type": "system", "subtype": "task_started", "task_id": tid, "description": "task " + tid, "task_type": "local_agent", "is_backgrounded": True})
        out({"type": "system", "subtype": "task_started", "task_id": "c1", "parent_task_id": "t1", "description": "child step", "task_type": "local_bash", "is_backgrounded": False})
        out({"type": "system", "subtype": "task_progress", "task_id": "t2", "description": "running tests"})
    text("reply:" + t)
    if t.startswith("done") and os.environ.get("FAKE_SLOW"):
        time.sleep(float(os.environ["FAKE_SLOW"]))
    if "permission" in t:
        out({"type": "control_request", "request_id": "perm-1", "request": {"subtype": "can_use_tool", "tool_name": "Bash", "input": {"command": "touch x"}}})
        out({"type": "control_request", "request_id": "other-1", "request": {"subtype": "hook_callback"}})
        got = {}
        deadline = time.time() + 5
        while len(got) < 2 and time.time() < deadline:
            try:
                l = q.get(timeout=0.02)
            except queue.Empty:
                continue
            m = json.loads(l)
            if m.get("type") == "control_response":
                got[m["response"]["request_id"]] = m["response"]
        perm = got.get("perm-1", {})
        text(" perm:%s/%s other:%s" % (perm.get("subtype"), perm.get("response", {}).get("behavior"), got.get("other-1", {}).get("subtype")))
    if absorb or "hang" in t:
        deadline = time.time() + (absorb or 30)
        while time.time() < deadline:
            try:
                l = q.get(timeout=0.02)
            except queue.Empty:
                continue
            if l is None:
                eof[0] = True
                break
            m = json.loads(l)
            if m["type"] == "user":
                text(" absorbed:" + msg_text(m))
                break
            if m["type"] == "control_request" and m["request"]["subtype"] == "interrupt":
                interrupt(m)
                return
    result()
while True:
    for name in os.listdir(D):
        if name == "crash":
            os.remove(os.path.join(D, name))
            os._exit(3)
        if name.startswith("complete_"):
            tid = name[len("complete_"):]
            os.remove(os.path.join(D, name))
            if tasks.get(tid) == "running":
                end_task(tid, "completed")
                turn("done " + tid + " BG-OK", float(os.environ.get("FAKE_ABSORB", "0")))
    if eof[0]:
        sys.exit(0)
    try:
        l = q.get(timeout=0.02)
    except queue.Empty:
        continue
    if l is None:
        eof[0] = True
        continue
    m = json.loads(l)
    if m["type"] == "user":
        turn(msg_text(m))
    elif m["type"] == "control_request":
        r = m["request"]
        if r["subtype"] == "interrupt":
            interrupt(m)
            continue
        out({"type": "control_response", "response": {"subtype": "success", "request_id": m["request_id"], "response": {}}})
        if r["subtype"] == "stop_task" and tasks.get(r["task_id"]) == "running":
            end_task(r["task_id"], "stopped")
            turn("stopped " + r["task_id"])
`

type liveEnv struct {
	t   *testing.T
	dir string
	srv *httptest.Server
}

func newLiveEnv(t *testing.T) *liveEnv {
	t.Helper()
	bin := t.TempDir()
	if err := os.WriteFile(filepath.Join(bin, "claude"), []byte(fakeLiveClaude), 0o755); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", bin+string(os.PathListSeparator)+os.Getenv("PATH"))
	dir := t.TempDir()
	t.Setenv("FAKE_DIR", dir)
	sessionStore = sessions.New(t.TempDir())
	oldMeter := resumeMeter
	resumeMeter = newSessionMeter(t.TempDir())
	oldInterval := liveFrameInterval
	liveFrameInterval = 50 * time.Millisecond
	mux := http.NewServeMux()
	mux.HandleFunc("/v1/chat/completions", chatCompletionsHandler)
	mux.HandleFunc("/live/attach", liveAttachHandler)
	mux.HandleFunc("/live/stop_task", liveStopTaskHandler)
	mux.HandleFunc("/live/close", liveCloseHandler)
	mux.HandleFunc("/live/list", liveListHandler)
	mux.HandleFunc("/health", healthHandler)
	srv := httptest.NewServer(mux)
	e := &liveEnv{t: t, dir: dir, srv: srv}
	t.Cleanup(func() {
		// End every live process this test left behind.
		liveSessions.Lock()
		var all []*liveSession
		for _, s := range liveSessions.m {
			all = append(all, s)
		}
		liveSessions.Unlock()
		for _, s := range all {
			s.close("reset")
			select {
			case <-s.done:
			case <-time.After(5 * time.Second):
			}
			dropLive(s)
		}
		srv.Close()
		resumeMeter = oldMeter
		liveFrameInterval = oldInterval
	})
	return e
}

func (e *liveEnv) touch(name string) {
	if err := os.WriteFile(filepath.Join(e.dir, name), nil, 0o644); err != nil {
		e.t.Fatal(err)
	}
}

func (e *liveEnv) stdinLog() string {
	data, _ := os.ReadFile(filepath.Join(e.dir, "stdin.log"))
	return string(data)
}

func (e *liveEnv) launches() int {
	data, _ := os.ReadFile(filepath.Join(e.dir, "launches"))
	return strings.Count(string(data), "x")
}

func chatBody(sessionID, owner, text string, deadline int64, extra map[string]any) []byte {
	body := map[string]any{
		"model": "m", "stream": true, "stream_options": map[string]bool{"include_usage": true},
		"session_id": sessionID, "live_owner": owner, "live_deadline": deadline,
		"messages": []map[string]string{{"role": "user", "content": text}},
	}
	for k, v := range extra {
		body[k] = v
	}
	b, _ := json.Marshal(body)
	return b
}

// sseStream reads an SSE response line by line in the background.
type sseStream struct {
	t     *testing.T
	resp  *http.Response
	lines chan string
	seen  []string
}

func (e *liveEnv) post(ctx context.Context, path string, body []byte) *http.Response {
	e.t.Helper()
	req, _ := http.NewRequestWithContext(ctx, http.MethodPost, e.srv.URL+path, bytes.NewReader(body))
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		e.t.Fatal(err)
	}
	return resp
}

func (e *liveEnv) stream(ctx context.Context, path string, body []byte) *sseStream {
	e.t.Helper()
	resp := e.post(ctx, path, body)
	if resp.StatusCode != http.StatusOK {
		e.t.Fatalf("%s: status %d", path, resp.StatusCode)
	}
	s := &sseStream{t: e.t, resp: resp, lines: make(chan string, 4096)}
	go func() {
		defer close(s.lines)
		sc := bufio.NewScanner(resp.Body)
		sc.Buffer(make([]byte, 0, 64*1024), 8*1024*1024)
		for sc.Scan() {
			if l := sc.Text(); l != "" {
				s.lines <- l
			}
		}
	}()
	e.t.Cleanup(func() { resp.Body.Close() })
	return s
}

// until reads lines until one contains want, returning everything read.
func (s *sseStream) until(want string, timeout time.Duration) string {
	s.t.Helper()
	deadline := time.After(timeout)
	start := len(s.seen)
	for {
		select {
		case l, ok := <-s.lines:
			if !ok {
				s.t.Fatalf("stream ended before %q; got:\n%s", want, strings.Join(s.seen[start:], "\n"))
			}
			s.seen = append(s.seen, l)
			if strings.Contains(l, want) {
				return strings.Join(s.seen[start:], "\n")
			}
		case <-deadline:
			s.t.Fatalf("no %q within %s; got:\n%s", want, timeout, strings.Join(s.seen[start:], "\n"))
		}
	}
}

// rest reads the stream to its end.
func (s *sseStream) rest(timeout time.Duration) string {
	s.t.Helper()
	deadline := time.After(timeout)
	start := len(s.seen)
	for {
		select {
		case l, ok := <-s.lines:
			if !ok {
				return strings.Join(s.seen[start:], "\n")
			}
			s.seen = append(s.seen, l)
		case <-deadline:
			s.t.Fatalf("stream still open after %s; got:\n%s", timeout, strings.Join(s.seen[start:], "\n"))
		}
	}
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(20 * time.Millisecond)
	}
	t.Fatalf("timed out waiting for %s", what)
}

func deadlineIn(d time.Duration) int64 { return time.Now().Add(d).Unix() }

// startLiveSession runs a first turn that spawns two background tasks and
// returns once the turn's stream is done.
func (e *liveEnv) startLiveSession(sid string, deadline int64) string {
	e.t.Helper()
	start := time.Now()
	chat := e.stream(context.Background(), "/v1/chat/completions", chatBody(sid, "u1", "spawn work", deadline, nil))
	body := chat.rest(5 * time.Second)
	if time.Since(start) > 4*time.Second {
		e.t.Fatalf("first turn took %s", time.Since(start))
	}
	if ceiling, _ := os.ReadFile(filepath.Join(e.dir, "ceiling")); len(ceiling) == 0 {
		e.t.Fatal("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS not set for a process with a deadline")
	}
	if lookupLive(sid) == nil {
		e.t.Fatalf("session %s not live after a turn with background tasks:\n%s", sid, body)
	}
	return body
}

func TestBGTrackerFollowsTaskEvents(t *testing.T) {
	tr := newBGTracker()
	tr.now = func() time.Time { return time.Unix(1000, 0) }
	feed := func(line string) bool { return tr.observe([]byte(line)) }
	// background_tasks_changed may list a task before its task_started.
	feed(`{"type":"system","subtype":"background_tasks_changed","tasks":[{"task_id":"a1","task_type":"local_agent","description":"orders export"}]}`)
	feed(`{"type":"system","subtype":"task_started","task_id":"a1","description":"orders export","task_type":"local_agent","is_backgrounded":true}`)
	feed(`{"type":"system","subtype":"task_started","task_id":"b1","description":"build","task_type":"local_bash","is_backgrounded":true}`)
	// A foreground subagent and a nested task are not background rows.
	feed(`{"type":"system","subtype":"task_started","task_id":"f1","description":"fg","task_type":"local_agent","is_backgrounded":false}`)
	feed(`{"type":"system","subtype":"background_tasks_changed","tasks":[{"task_id":"a1"},{"task_id":"b1"},{"task_id":"n1","task_type":"local_bash"}]}`)
	feed(`{"type":"system","subtype":"task_started","task_id":"n1","parent_task_id":"a1","description":"run backend tests","task_type":"local_bash","is_backgrounded":false,"owned_by_subagent":true}`)
	feed(`{"type":"system","subtype":"task_progress","task_id":"n1","description":"` + strings.Repeat("x", 100) + `"}`)

	tasks, _ := tr.snapshot()
	if len(tasks) != 2 || tasks[0].ID != "a1" || tasks[1].ID != "b1" {
		t.Fatalf("tasks = %+v, want a1 and b1", tasks)
	}
	if tasks[0].Kind != "agent" || tasks[1].Kind != "bash" || tasks[0].Status != "running" || tasks[0].StartedAt != 1000 {
		t.Fatalf("task fields = %+v", tasks)
	}
	if n := len([]rune(tasks[0].LastProgress)); n != lastProgressRunes || tasks[0].LastProgressAt == nil {
		t.Fatalf("nested progress not attributed to the parent (len %d): %+v", n, tasks[0])
	}

	feed(`{"type":"system","subtype":"task_updated","task_id":"b1","patch":{"status":"killed","end_time":1500000}}`)
	feed(`{"type":"system","subtype":"task_notification","task_id":"n1","status":"completed"}`)
	if got := tr.running(); len(got) != 1 || got[0] != "a1" {
		t.Fatalf("running = %v, want [a1]", got)
	}
	tasks, _ = tr.snapshot()
	if tasks[1].Status != "stopped" || tasks[1].EndedAt == nil || *tasks[1].EndedAt != 1500 {
		t.Fatalf("b1 = %+v, want stopped at 1500", tasks[1])
	}
	feed(`{"type":"system","subtype":"task_notification","task_id":"a1","status":"completed"}`)
	if len(tr.running()) != 0 {
		t.Fatalf("running = %v, want none", tr.running())
	}
	v := tr.currentVersion()
	if feed(`{"type":"system","subtype":"status","status":"requesting"}`) || tr.currentVersion() != v {
		t.Fatal("unrelated system event changed the table")
	}
}

// With stdin open the CLI waits for answers to its control requests: the
// relay denies permission prompts and refuses anything else at once, and the
// turn goes on.
func TestLiveAnswersCLIControlRequests(t *testing.T) {
	e := newLiveEnv(t)
	chat := e.stream(context.Background(), "/v1/chat/completions", chatBody("sess-perm", "u1", "permission please", deadlineIn(time.Hour), nil))
	body := chat.rest(8 * time.Second)
	if !strings.Contains(body, "perm:success/deny other:error") || !strings.Contains(body, "data: [DONE]") {
		t.Fatalf("control requests not answered:\n%s", body)
	}
	if strings.Contains(body, "control_request") {
		t.Fatalf("control_request leaked into the stream:\n%s", body)
	}
	log := e.stdinLog()
	for _, want := range []string{`"request_id":"perm-1"`, `"behavior":"deny"`, `"request_id":"other-1"`, `"error":"not supported by relay"`} {
		if !strings.Contains(log, want) {
			t.Fatalf("stdin missing %s:\n%s", want, log)
		}
	}
}

// A turn without background tasks ends as before: stdin closes at the
// result, the process exits, and nothing stays live.
func TestLiveTurnWithoutBackgroundEndsProcess(t *testing.T) {
	e := newLiveEnv(t)
	chat := e.stream(context.Background(), "/v1/chat/completions", chatBody("sess-plain", "u1", "hello", deadlineIn(time.Hour), nil))
	body := chat.rest(5 * time.Second)
	if !strings.Contains(body, "reply:Human: hello") || !strings.Contains(body, "data: [DONE]") {
		t.Fatalf("body:\n%s", body)
	}
	if strings.Contains(body, `"background"`) {
		t.Fatalf("background frame without background tasks:\n%s", body)
	}
	if lookupLive("sess-plain") != nil {
		t.Fatal("session live without background tasks")
	}
}

func TestLiveSessionLifecycle(t *testing.T) {
	e := newLiveEnv(t)
	deadline := deadlineIn(time.Hour)
	first := e.startLiveSession("sess-live", deadline)
	for _, want := range []string{`"background_tasks":[`, `"background":{"deadline":` + fmt.Sprint(deadline), `"finish_reason":"stop"`, `"usage":`, "data: [DONE]"} {
		if !strings.Contains(first, want) {
			t.Fatalf("first turn missing %s:\n%s", want, first)
		}
	}
	if strings.Index(first, `"background":{`) > strings.Index(first, `"finish_reason":"stop"`) {
		t.Fatalf("background frame must precede the finish chunk:\n%s", first)
	}

	// /live/list and /health see it.
	resp, _ := http.Get(e.srv.URL + "/live/list")
	var list struct {
		Sessions []struct {
			SessionID string     `json:"session_id"`
			Owner     string     `json:"owner"`
			Deadline  int64      `json:"deadline"`
			Busy      bool       `json:"busy"`
			Tasks     []liveTask `json:"tasks"`
		} `json:"sessions"`
	}
	json.NewDecoder(resp.Body).Decode(&list)
	resp.Body.Close()
	if len(list.Sessions) != 1 || list.Sessions[0].Owner != "u1" || list.Sessions[0].Deadline != deadline || len(list.Sessions[0].Tasks) != 2 || list.Sessions[0].Busy {
		t.Fatalf("list = %+v", list)
	}
	if list.Sessions[0].Tasks[0].LastProgress != "child step" || list.Sessions[0].Tasks[1].LastProgress != "running tests" {
		t.Fatalf("progress = %+v", list.Sessions[0].Tasks)
	}

	attach := e.stream(context.Background(), "/live/attach", []byte(`{"session_id":"sess-live"}`))
	if got := attach.until(`"background_tasks"`, 2*time.Second); !strings.Contains(got, `"id":"t1"`) {
		t.Fatalf("attach snapshot:\n%s", got)
	}

	// A background task finishes: the continued turn streams on attach.
	e.touch("complete_t1")
	got := attach.until(`"turn_end"`, 5*time.Second)
	for _, want := range []string{"done t1 BG-OK", `"usage":{"prompt_tokens":30,"completion_tokens":10`, `"turn_end":{"merged":false}`, `"status":"completed"`} {
		if !strings.Contains(got, want) {
			t.Fatalf("attach turn missing %s:\n%s", want, got)
		}
	}

	// Another user cannot write into this process.
	resp = e.post(context.Background(), "/v1/chat/completions", chatBody("sess-live", "u2", "hi", deadline, nil))
	var errBody struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	json.NewDecoder(resp.Body).Decode(&errBody)
	resp.Body.Close()
	if resp.StatusCode != http.StatusConflict || errBody.Error.Code != "live_owner_mismatch" {
		t.Fatalf("owner mismatch: status %d code %q", resp.StatusCode, errBody.Error.Code)
	}

	// The asker joins: the message goes to the same process.
	chat := e.stream(context.Background(), "/v1/chat/completions", chatBody("sess-live", "u1", "how is it going", deadline, map[string]any{"live_join": true}))
	body := chat.rest(5 * time.Second)
	for _, want := range []string{`"live_joined":{"tasks":[`, "reply:Human: how is it going", `"background":{`, `"completion_tokens":10`, "data: [DONE]"} {
		if !strings.Contains(body, want) {
			t.Fatalf("joined turn missing %s:\n%s", want, body)
		}
	}
	if !strings.Contains(e.stdinLog(), "how is it going") || e.launches() != 1 {
		t.Fatalf("joined message not written to the one process (launches=%d):\n%s", e.launches(), e.stdinLog())
	}
	if got := attach.until(`"turn_end"`, 2*time.Second); !strings.Contains(got, `"merged":true`) || strings.Contains(got, "how is it going") {
		t.Fatalf("attach for a joined turn:\n%s", got)
	}

	// stop_task stops only t2; with nothing left running the process ends.
	resp = e.post(context.Background(), "/live/stop_task", []byte(`{"session_id":"sess-live","task_id":"t2"}`))
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("stop_task status %d", resp.StatusCode)
	}
	end := attach.rest(5 * time.Second)
	if !strings.Contains(e.stdinLog(), `"request":{"subtype":"stop_task","task_id":"t2"}`) {
		t.Fatalf("stop_task control line missing:\n%s", e.stdinLog())
	}
	for _, want := range []string{"stopped t2", `"background_end":{"state":"finished"`, `"status":"stopped"`, "data: [DONE]"} {
		if !strings.Contains(end, want) {
			t.Fatalf("attach end missing %s:\n%s", want, end)
		}
	}
	if strings.Contains(end, "control_response") {
		t.Fatalf("control_response leaked:\n%s", end)
	}
	waitFor(t, "tombstone", func() bool { s := lookupLive("sess-live"); return s != nil && s.isEnded() })

	// After the phase the next turn starts a new process as before.
	next := e.stream(context.Background(), "/v1/chat/completions", chatBody("sess-live", "u1", "again", deadline, nil))
	if body := next.rest(5 * time.Second); !strings.Contains(body, "reply:Human: again") || e.launches() != 2 {
		t.Fatalf("next turn (launches=%d):\n%s", e.launches(), body)
	}
}

// live_join never starts a process: the request carries no credentials.
func TestLiveJoinWithoutLiveSession(t *testing.T) {
	e := newLiveEnv(t)
	resp := e.post(context.Background(), "/v1/chat/completions", chatBody("sess-none", "u1", "hi", deadlineIn(time.Hour), map[string]any{"live_join": true}))
	var errBody struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	json.NewDecoder(resp.Body).Decode(&errBody)
	resp.Body.Close()
	if resp.StatusCode != http.StatusConflict || errBody.Error.Code != "not_live" {
		t.Fatalf("status %d code %q, want 409 not_live", resp.StatusCode, errBody.Error.Code)
	}
	time.Sleep(200 * time.Millisecond)
	if e.launches() != 0 {
		t.Fatalf("live_join started %d process(es)", e.launches())
	}
	for _, path := range []string{"/live/attach", "/live/stop_task", "/live/close"} {
		resp := e.post(context.Background(), path, []byte(`{"session_id":"sess-none","task_id":"x","reason":"user"}`))
		resp.Body.Close()
		if resp.StatusCode != http.StatusNotFound {
			t.Fatalf("%s status %d, want 404", path, resp.StatusCode)
		}
	}
}

// A message written while a continued turn runs is absorbed into it: the
// whole turn answers the chat request and attach only learns it merged.
func TestLiveMergedTurnRoutesToChat(t *testing.T) {
	e := newLiveEnv(t)
	t.Setenv("FAKE_ABSORB", "3")
	e.startLiveSession("sess-merge", deadlineIn(time.Hour))
	attach := e.stream(context.Background(), "/live/attach", []byte(`{"session_id":"sess-merge"}`))
	attach.until(`"background_tasks"`, 2*time.Second)
	e.touch("complete_t1")
	attach.until("done t1 BG-OK", 5*time.Second)

	chat := e.stream(context.Background(), "/v1/chat/completions", chatBody("sess-merge", "u1", "status?", deadlineIn(time.Hour), nil))
	body := chat.rest(5 * time.Second)
	if !strings.Contains(body, "absorbed:Human: status?") || !strings.Contains(body, `"finish_reason":"stop"`) {
		t.Fatalf("chat did not get the merged turn:\n%s", body)
	}
	got := attach.until(`"turn_end"`, 2*time.Second)
	if !strings.Contains(got, `"turn_end":{"merged":false,"partial":true}`) || strings.Contains(got, `"usage"`) {
		t.Fatalf("attach did not close its part of the turn as partial:\n%s", got)
	}
	got = attach.until(`"turn_end"`, 2*time.Second)
	if !strings.Contains(got, `"merged":true`) || strings.Contains(got, "absorbed") {
		t.Fatalf("attach for a merged turn:\n%s", got)
	}
}

// If the CLI runs the message as its own turn after the running one, the
// running turn's result closes it on attach and the chat waits for its turn.
func TestLiveJoinedChatWaitsForItsOwnTurn(t *testing.T) {
	e := newLiveEnv(t)
	t.Setenv("FAKE_SLOW", "1")
	e.startLiveSession("sess-own", deadlineIn(time.Hour))
	attach := e.stream(context.Background(), "/live/attach", []byte(`{"session_id":"sess-own"}`))
	attach.until(`"background_tasks"`, 2*time.Second)
	e.touch("complete_t1")
	attach.until("done t1 BG-OK", 5*time.Second)

	chat := e.stream(context.Background(), "/v1/chat/completions", chatBody("sess-own", "u1", "next please", deadlineIn(time.Hour), nil))
	body := chat.rest(8 * time.Second)
	if !strings.Contains(body, "reply:Human: next please") || strings.Count(body, `"finish_reason":"stop"`) != 1 {
		t.Fatalf("chat did not get its own turn:\n%s", body)
	}
	got := attach.until(`"merged":true`, 3*time.Second)
	partial := strings.Index(got, `"partial":true`)
	ended := strings.Index(got, `"turn_end":{"merged":false}`)
	if partial < 0 || ended < partial || !strings.Contains(got[partial:ended], `"usage"`) || strings.Contains(got, "next please") {
		t.Fatalf("attach frames around the joined turn:\n%s", got)
	}
}

func liveBacklogHas(sid, want string) func() bool {
	return func() bool {
		s := lookupLive(sid)
		if s == nil {
			return false
		}
		s.mu.Lock()
		defer s.mu.Unlock()
		for _, f := range s.backlog {
			if strings.Contains(string(f.b), want) {
				return true
			}
		}
		return false
	}
}

// Without an attach subscriber the frames wait in the backlog and are
// replayed on attach.
func TestLiveBacklogReplayedOnAttach(t *testing.T) {
	e := newLiveEnv(t)
	e.startLiveSession("sess-backlog", deadlineIn(time.Hour))
	e.touch("complete_t1")
	waitFor(t, "turn in the backlog", liveBacklogHas("sess-backlog", `"turn_end"`))
	attach := e.stream(context.Background(), "/live/attach", []byte(`{"session_id":"sess-backlog"}`))
	got := attach.until(`"turn_end"`, 2*time.Second)
	if !strings.Contains(got, "done t1 BG-OK") || !strings.Contains(got, `"usage"`) {
		t.Fatalf("backlog replay:\n%s", got)
	}
	attach.until(`"background_tasks"`, 2*time.Second)
}

func TestLiveBacklogCapKeepsBoundaries(t *testing.T) {
	s := &liveSession{id: "cap"}
	old := liveBacklogBytes
	liveBacklogBytes = 100
	defer func() { liveBacklogBytes = old }()
	s.sendAttach([]byte("tasks-1"), true)
	for i := 0; i < 20; i++ {
		s.sendAttach([]byte(fmt.Sprintf("content-%02d", i)), false)
	}
	s.sendAttach([]byte("turn_end"), true)
	var got []string
	for _, f := range s.backlog {
		got = append(got, string(f.b))
	}
	joined := strings.Join(got, ",")
	if s.backlogBytes > 100 || !strings.HasPrefix(joined, "tasks-1,") || !strings.HasSuffix(joined, "content-19,turn_end") || strings.Contains(joined, "content-00") {
		t.Fatalf("backlog = %s (%d bytes)", joined, s.backlogBytes)
	}
}

// An ended session stays a tombstone: a late attach gets the backlog and
// background_end, controls are no-ops, live_join is not_live, and a normal
// turn starts a new process.
func TestLiveTombstone(t *testing.T) {
	e := newLiveEnv(t)
	e.startLiveSession("sess-tomb", deadlineIn(time.Hour))
	e.touch("complete_t1")
	waitFor(t, "first completion", liveBacklogHas("sess-tomb", "done t1"))
	e.touch("complete_t2")
	waitFor(t, "process end", func() bool { s := lookupLive("sess-tomb"); return s != nil && s.isEnded() })

	if n := liveCount(); n != 0 {
		t.Fatalf("liveCount = %d with only a tombstone", n)
	}
	resp, _ := http.Get(e.srv.URL + "/live/list")
	var list struct {
		Sessions []any `json:"sessions"`
	}
	json.NewDecoder(resp.Body).Decode(&list)
	resp.Body.Close()
	if len(list.Sessions) != 0 {
		t.Fatalf("/live/list lists a tombstone: %+v", list)
	}
	for _, path := range []string{"/live/stop_task", "/live/close"} {
		resp := e.post(context.Background(), path, []byte(`{"session_id":"sess-tomb","task_id":"t1","reason":"user"}`))
		var ok map[string]bool
		json.NewDecoder(resp.Body).Decode(&ok)
		resp.Body.Close()
		if resp.StatusCode != http.StatusOK || !ok["ok"] {
			t.Fatalf("%s on a tombstone: %d %v", path, resp.StatusCode, ok)
		}
	}

	attach := e.stream(context.Background(), "/live/attach", []byte(`{"session_id":"sess-tomb"}`))
	end := attach.rest(3 * time.Second)
	for _, want := range []string{"done t1 BG-OK", "done t2 BG-OK", `"background_end":{"state":"finished"`, "data: [DONE]"} {
		if !strings.Contains(end, want) {
			t.Fatalf("tombstone attach missing %s:\n%s", want, end)
		}
	}
	if strings.Index(end, "done t2") > strings.Index(end, "background_end") {
		t.Fatalf("background_end before the backlog:\n%s", end)
	}

	// Another user's live_join on it: not_live, not live_owner_mismatch.
	resp = e.post(context.Background(), "/v1/chat/completions", chatBody("sess-tomb", "u2", "hi", deadlineIn(time.Hour), map[string]any{"live_join": true}))
	var errBody struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	json.NewDecoder(resp.Body).Decode(&errBody)
	resp.Body.Close()
	if resp.StatusCode != http.StatusConflict || errBody.Error.Code != "not_live" {
		t.Fatalf("live_join on a tombstone: %d %q", resp.StatusCode, errBody.Error.Code)
	}

	chat := e.stream(context.Background(), "/v1/chat/completions", chatBody("sess-tomb", "u2", "fresh", deadlineIn(time.Hour), nil))
	if body := chat.rest(5 * time.Second); !strings.Contains(body, "reply:Human: fresh") || e.launches() != 2 {
		t.Fatalf("new turn after the tombstone (launches=%d):\n%s", e.launches(), body)
	}
	if lookupLive("sess-tomb") != nil {
		t.Fatal("tombstone not replaced by the new turn")
	}
}

// An ending session answers not_live before checking the owner.
func TestLiveEndingAnswersNotLiveBeforeOwner(t *testing.T) {
	e := newLiveEnv(t)
	e.startLiveSession("sess-ending", deadlineIn(time.Hour))
	lookupLive("sess-ending").setEndState("stopped")
	resp := e.post(context.Background(), "/v1/chat/completions", chatBody("sess-ending", "u2", "hi", deadlineIn(time.Hour), map[string]any{"live_join": true}))
	var errBody struct {
		Error struct {
			Code string `json:"code"`
		} `json:"error"`
	}
	json.NewDecoder(resp.Body).Decode(&errBody)
	resp.Body.Close()
	if resp.StatusCode != http.StatusConflict || errBody.Error.Code != "not_live" {
		t.Fatalf("ending session: %d %q, want 409 not_live", resp.StatusCode, errBody.Error.Code)
	}
}

// Leaving a joined turn interrupts it (and with it every background task).
func TestLiveChatDisconnectInterrupts(t *testing.T) {
	e := newLiveEnv(t)
	e.startLiveSession("sess-stop", deadlineIn(time.Hour))
	attach := e.stream(context.Background(), "/live/attach", []byte(`{"session_id":"sess-stop"}`))
	attach.until(`"background_tasks"`, 2*time.Second)

	ctx, cancel := context.WithCancel(context.Background())
	chat := e.stream(ctx, "/v1/chat/completions", chatBody("sess-stop", "u1", "hang on this", deadlineIn(time.Hour), nil))
	chat.until("reply:Human: hang on this", 5*time.Second)
	cancel()

	end := attach.rest(5 * time.Second)
	if !strings.Contains(e.stdinLog(), `"request":{"subtype":"interrupt"}`) {
		t.Fatalf("interrupt not sent:\n%s", e.stdinLog())
	}
	if !strings.Contains(end, `"background_end":{"state":"stopped"`) {
		t.Fatalf("attach end:\n%s", end)
	}
}

// The relay closes a session at its deadline: stop_task for each running
// task, then stdin; attach learns it expired.
func TestLiveDeadlineExpires(t *testing.T) {
	e := newLiveEnv(t)
	e.startLiveSession("sess-exp", deadlineIn(2*time.Second))
	attach := e.stream(context.Background(), "/live/attach", []byte(`{"session_id":"sess-exp"}`))
	end := attach.rest(8 * time.Second)
	if !strings.Contains(end, `"background_end":{"state":"expired"`) {
		t.Fatalf("attach end:\n%s", end)
	}
	log := e.stdinLog()
	if !strings.Contains(log, `"task_id":"t1"`) || !strings.Contains(log, `"task_id":"t2"`) {
		t.Fatalf("stop_task not sent for each task:\n%s", log)
	}
}

func TestLiveCloseByUser(t *testing.T) {
	e := newLiveEnv(t)
	e.startLiveSession("sess-close", deadlineIn(time.Hour))
	attach := e.stream(context.Background(), "/live/attach", []byte(`{"session_id":"sess-close"}`))
	attach.until(`"background_tasks"`, 2*time.Second)
	resp := e.post(context.Background(), "/live/close", []byte(`{"session_id":"sess-close","reason":"user"}`))
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("close status %d", resp.StatusCode)
	}
	if end := attach.rest(5 * time.Second); !strings.Contains(end, `"background_end":{"state":"stopped"`) {
		t.Fatalf("attach end:\n%s", end)
	}
	waitFor(t, "tombstone", func() bool { s := lookupLive("sess-close"); return s != nil && s.isEnded() })
}

func TestLiveProcessCrashFails(t *testing.T) {
	e := newLiveEnv(t)
	e.startLiveSession("sess-crash", deadlineIn(time.Hour))
	attach := e.stream(context.Background(), "/live/attach", []byte(`{"session_id":"sess-crash"}`))
	attach.until(`"background_tasks"`, 2*time.Second)
	e.touch("crash")
	if end := attach.rest(5 * time.Second); !strings.Contains(end, `"background_end":{"state":"failed"`) {
		t.Fatalf("attach end:\n%s", end)
	}
	waitFor(t, "tombstone", func() bool { s := lookupLive("sess-crash"); return s != nil && s.isEnded() })
}

// One attach stream per session: a new one ends the old one. Live streams
// carry keepalives.
func TestLiveAttachReplacedAndKeepalive(t *testing.T) {
	e := newLiveEnv(t)
	old := liveKeepalive
	liveKeepalive = 100 * time.Millisecond
	defer func() { liveKeepalive = old }()
	e.startLiveSession("sess-attach", deadlineIn(time.Hour))
	a1 := e.stream(context.Background(), "/live/attach", []byte(`{"session_id":"sess-attach"}`))
	a1.until(": keepalive", 2*time.Second)
	a2 := e.stream(context.Background(), "/live/attach", []byte(`{"session_id":"sess-attach"}`))
	a2.until(`"background_tasks"`, 2*time.Second)
	if end := a1.rest(2 * time.Second); !strings.Contains(end, "data: [DONE]") {
		t.Fatalf("replaced attach did not end with [DONE]:\n%s", end)
	}
	if lookupLive("sess-attach") == nil {
		t.Fatal("replacing the attach stream ended the session")
	}
}

func TestLiveCeilingEnvAndHealth(t *testing.T) {
	if got := liveCeilingEnv(1000, time.Unix(400, 0)); got != "900000" {
		t.Fatalf("ceiling = %s, want 900000", got)
	}
	if got := liveCeilingEnv(100, time.Unix(1000, 0)); got != "1000" {
		t.Fatalf("past deadline ceiling = %s", got)
	}
	e := newLiveEnv(t)
	resp, err := http.Get(e.srv.URL + "/health")
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var health map[string]any
	json.NewDecoder(resp.Body).Decode(&health)
	if health["live_sessions"] != true || health["live_count"] != float64(0) {
		t.Fatalf("health = %v", health)
	}
}
