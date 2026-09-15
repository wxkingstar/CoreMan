package main

import (
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

func TestZeroStdoutAuthErrorPreservesSession(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "codex"), []byte("#!/bin/sh\necho 'authentication failed' >&2\nexit 1\n"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)
	rebuilt := false
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), codexInput{IsResume: true}, "r", 1, "m", false, dir, nil, "existing", func() codexInput { rebuilt = true; return codexInput{} })
	if rebuilt {
		t.Fatal("must not destroy session on auth failure")
	}
	body := rec.Body.String()
	if !strings.Contains(body, `"x_relay_error":true`) {
		t.Fatal(body)
	}
	// 失败也要有确认终态：否则下游按「流被截断」处理，原因文本被通用文案顶掉。
	errAt := strings.Index(body, "[codex error]")
	finishAt := strings.Index(body, `"finish_reason":"stop"`)
	doneAt := strings.Index(body, "data: [DONE]")
	if errAt < 0 || finishAt < errAt || doneAt < finishAt {
		t.Fatalf("want error delta, finish chunk, [DONE] in order; got:\n%s", body)
	}
}

func TestCodexHeadersBeforeFirstOutput(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "codex"), []byte("#!/bin/sh\nexec /bin/sleep 30\n"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		handleStreamResponse(w, r, codexInput{}, "r", 1, "m", false, dir, nil, "", nil)
	}))
	defer srv.Close()
	client := &http.Client{Timeout: time.Second}
	response, err := client.Get(srv.URL)
	if err != nil {
		t.Fatal(err)
	}
	if response.StatusCode != 200 {
		t.Fatal(response.StatusCode)
	}
	response.Body.Close()
}

func TestThreadWriteFailureKeepsPublishedMapping(t *testing.T) {
	root := t.TempDir()
	tm := newThreadMap(root)
	if err := tm.Set("s", "old"); err != nil {
		t.Fatal(err)
	}
	bad := filepath.Join(root, "not-a-directory")
	os.WriteFile(bad, []byte("x"), 0600)
	tm.rootDir = bad
	if err := tm.Set("s", "new"); err == nil {
		t.Fatal("expected write failure")
	}
	if got := tm.Get("s"); got != "old" {
		t.Fatal(got)
	}
	restarted := newThreadMap(root)
	if got := restarted.Get("s"); got != "old" {
		t.Fatal(got)
	}
}

func TestPartialOutputWithoutCompletionFails(t *testing.T) {
	for _, stream := range []bool{true, false} {
		t.Run(fmt.Sprint(stream), func(t *testing.T) {
			dir := t.TempDir()
			os.WriteFile(filepath.Join(dir, "codex"), []byte("#!/bin/sh\necho '{\"type\":\"item.completed\",\"item\":{\"type\":\"agent_message\",\"text\":\"partial\"}}'\nexit 1\n"), 0700)
			t.Setenv("PATH", dir)
			rec := httptest.NewRecorder()
			req := httptest.NewRequest("POST", "/", nil)
			if stream {
				handleStreamResponse(rec, req, codexInput{}, "r", 1, "m", false, dir, nil, "", nil)
				if !strings.Contains(rec.Body.String(), `"finish_reason":"error"`) {
					t.Fatal(rec.Body.String())
				}
			} else {
				handleNonStreamResponse(rec, req, codexInput{}, "r", 1, "m", dir, nil, "", nil)
				if rec.Code != 500 {
					t.Fatal(rec.Code, rec.Body.String())
				}
			}
			if !strings.Contains(rec.Body.String(), "incomplete_result") {
				t.Fatal(rec.Body.String())
			}
		})
	}
}

// fakeCodex installs a codex stub on PATH for the duration of the test.
func fakeCodex(t *testing.T, script string) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "codex"), []byte("#!/bin/sh\n"+script), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)
	return dir
}

// withSessionState swaps in real thread bindings and a session store, which
// the handlers touch once codex reports a thread or an answer.
func withSessionState(t *testing.T) {
	t.Helper()
	oldThreads, oldStore := threads, sessionStore
	threads = newThreadMap(t.TempDir())
	sessionStore = sessions.New(t.TempDir())
	t.Cleanup(func() { threads, sessionStore = oldThreads, oldStore })
}

// missingRolloutOnResume mimics codex after its session store was wiped:
// resuming exits 1 with the rollout error on stderr only, a fresh run answers.
const missingRolloutOnResume = `for a in "$@"; do
  if [ "$a" = resume ]; then echo 'Error: no rollout found for thread id dead-thread' >&2; exit 1; fi
done
/bin/cat >/dev/null
echo '{"type":"thread.started","thread_id":"fresh-thread"}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"hello again"}}'
echo '{"type":"turn.completed"}'
`

func resumeInput() codexInput {
	return codexInput{IsResume: true, Args: []string{"exec", "resume", "dead-thread", "--json", "-"}}
}

func countingRebuild(counter *int) func() codexInput {
	return func() codexInput {
		*counter++
		if threads != nil {
			threads.Forget("s1")
		}
		return codexInput{Args: []string{"exec", "--json", "-"}}
	}
}

func TestZeroStdoutMissingRolloutStartsNewThread(t *testing.T) {
	dir := fakeCodex(t, missingRolloutOnResume)
	withSessionState(t)
	if err := threads.Set("s1", "dead-thread"); err != nil {
		t.Fatal(err)
	}
	rebuilt := 0
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), resumeInput(), "r", 1, "m", false, dir, nil, "s1", countingRebuild(&rebuilt))
	body := rec.Body.String()
	if rebuilt != 1 {
		t.Fatalf("rebuilt %d times; body:\n%s", rebuilt, body)
	}
	noticeAt := strings.Index(body, strings.TrimSpace(staleThreadNotice))
	answerAt := strings.Index(body, "hello again")
	if noticeAt < 0 || answerAt < noticeAt {
		t.Fatalf("notice must precede the answer; body:\n%s", body)
	}
	if strings.Contains(body, "x_relay_error") || !strings.Contains(body, `"finish_reason":"stop"`) {
		t.Fatalf("the rebuilt turn should succeed; body:\n%s", body)
	}
	if got := threads.Get("s1"); got != "fresh-thread" {
		t.Fatalf("binding = %q, want fresh-thread", got)
	}
}

func TestNonStreamMissingRolloutStartsNewThread(t *testing.T) {
	dir := fakeCodex(t, missingRolloutOnResume)
	withSessionState(t)
	rebuilt := 0
	rec := httptest.NewRecorder()
	handleNonStreamResponse(rec, httptest.NewRequest("POST", "/", nil), resumeInput(), "r", 1, "m", dir, nil, "s1", countingRebuild(&rebuilt))
	body := rec.Body.String()
	if rec.Code != http.StatusOK || rebuilt != 1 {
		t.Fatalf("code=%d rebuilt=%d body=%s", rec.Code, rebuilt, body)
	}
	if !strings.Contains(body, strings.TrimSpace(staleThreadNotice)) || !strings.Contains(body, "hello again") {
		t.Fatal(body)
	}
}

func TestZeroStdoutWithoutMissingRolloutKeepsBinding(t *testing.T) {
	cases := map[string]struct {
		script string
		input  codexInput
	}{
		"auth failure on resume":         {"echo 'authentication failed' >&2\nexit 1\n", resumeInput()},
		"missing rollout without resume": {"echo 'Error: no rollout found for thread id x' >&2\nexit 1\n", codexInput{Args: []string{"exec", "--json", "-"}}},
	}
	for name, tc := range cases {
		t.Run(name, func(t *testing.T) {
			dir := fakeCodex(t, tc.script)
			rebuilt := 0
			rec := httptest.NewRecorder()
			handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), tc.input, "r", 1, "m", false, dir, nil, "s1", countingRebuild(&rebuilt))
			body := rec.Body.String()
			if rebuilt != 0 {
				t.Fatalf("must not rebuild; body:\n%s", body)
			}
			if !strings.Contains(body, `"x_relay_error":true`) || !strings.Contains(body, "reset") {
				t.Fatalf("want a structured error that mentions reset; body:\n%s", body)
			}
		})
	}
}

func TestRebuiltThreadThatAlsoFailsIsReported(t *testing.T) {
	dir := fakeCodex(t, "echo 'Error: no rollout found for thread id dead-thread' >&2\nexit 1\n")
	withSessionState(t)
	rebuilt := 0
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), resumeInput(), "r", 1, "m", false, dir, nil, "s1", countingRebuild(&rebuilt))
	body := rec.Body.String()
	if rebuilt != 1 || !strings.Contains(body, `"x_relay_error":true`) || !strings.Contains(body, "新会话也未能启动") {
		t.Fatalf("rebuilt=%d body:\n%s", rebuilt, body)
	}
}

func TestStaleThreadSignatureIsMatchedPerStderrLine(t *testing.T) {
	rebuild := func() codexInput { return codexInput{} }
	if canRebuildStaleThread(resumeInput(), rebuild, "worker thread started\nconfig file not found\n") {
		t.Fatal("words from separate stderr lines must not combine into a stale-thread verdict")
	}
	if !canRebuildStaleThread(resumeInput(), rebuild, "WARN retrying\nError: no rollout found for thread id abc\n") {
		t.Fatal("the verified rollout signature must be recognised")
	}
	if canRebuildStaleThread(resumeInput(), nil, "no rollout found for thread id abc") {
		t.Fatal("no rebuild without a rebuild closure")
	}
}

func TestTailBufferKeepsOnlyTheEnd(t *testing.T) {
	tb := &tailBuffer{limit: 8}
	tb.add("0123456789")
	tb.add("ab")
	if got := tb.String(); got != "6789\nab\n" {
		t.Fatalf("tail = %q", got)
	}
}
