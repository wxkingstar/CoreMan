package main

import (
	"context"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"testing"
	"time"
)

// writeContextRollout puts a rollout for threadID under a fresh CODEX_HOME whose last
// token_count reports used of window tokens, and returns its path.
func writeContextRollout(t *testing.T, threadID string, used, window int) string {
	t.Helper()
	home := t.TempDir()
	t.Setenv("CODEX_HOME", home)
	dir := filepath.Join(home, "sessions", "2026", "11", "02")
	if err := os.MkdirAll(dir, 0700); err != nil {
		t.Fatal(err)
	}
	path := filepath.Join(dir, "rollout-2026-11-02T13-00-00-"+threadID+".jsonl")
	body := `{"type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"total_tokens":` +
		strconv.Itoa(used) + `},"model_context_window":` + strconv.Itoa(window) + `}}}` + "\n" +
		`{"type":"event_msg","payload":{"type":"token_count","info":null,"rate_limits":{}}}` + "\n"
	if err := os.WriteFile(path, []byte(body), 0600); err != nil {
		t.Fatal(err)
	}
	rolloutPaths.Delete(threadID)
	return path
}

func TestRolloutContextUseSkipsTokenCountsWithoutInfo(t *testing.T) {
	path := writeContextRollout(t, "t-use", 400000, 475000)
	if used, window := rolloutContextUse(path); used != 400000 || window != 475000 {
		t.Fatal(used, window)
	}
	if used, window := rolloutContextUse(""); used != 0 || window != 0 {
		t.Fatal(used, window)
	}
}

// stubCompaction replaces the app-server call with one that blocks until the
// returned channel is closed.
func stubCompaction(t *testing.T) (release chan struct{}, calls *int) {
	t.Helper()
	release, calls = make(chan struct{}), new(int)
	old := runCompaction
	runCompaction = func(ctx context.Context, threadID, path, workingDir, model string) error {
		*calls++
		<-release
		return nil
	}
	t.Cleanup(func() { runCompaction = old })
	return release, calls
}

func TestEnsureCompactionOnlyAboveThreshold(t *testing.T) {
	release, calls := stubCompaction(t)
	writeContextRollout(t, "t-low", 300000, 475000)
	if job := ensureCompaction("t-low", "", ""); job != nil {
		t.Fatal("63% of the window must not compact")
	}
	writeContextRollout(t, "t-high", 400000, 475000)
	job := ensureCompaction("t-high", "", "")
	if job == nil {
		t.Fatal("84% of the window must compact")
	}
	if again := ensureCompaction("t-high", "", ""); again != job {
		t.Fatal("a running compaction is shared, not started twice")
	}
	close(release)
	<-job.done
	if *calls != 1 {
		t.Fatal(*calls)
	}
}

func TestLeavingClientDoesNotStopCompaction(t *testing.T) {
	release, _ := stubCompaction(t)
	writeContextRollout(t, "t-leave", 400000, 475000)
	job := ensureCompaction("t-leave", "", "")
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if awaitCompaction(ctx, job, nil) {
		t.Fatal("a gone client must stop waiting")
	}
	if ensureCompaction("t-leave", "", "") != job {
		t.Fatal("the compaction must keep running for the next turn")
	}
	close(release)
	if !awaitCompaction(context.Background(), job, nil) {
		t.Fatal("must report completion")
	}
}

// codexWithAppServer answers `codex app-server` like codex-cli 0.164.0 does on
// a compaction (writing a fresh token_count to the rollout), and `codex exec`
// with a retry notice followed by a normal answer.
const codexWithAppServer = `if [ "$1" = app-server ]; then
  while read line; do
    case "$line" in
      *'"method":"initialize"'*) echo '{"id":1,"result":{}}' ;;
      *'"method":"thread/resume"'*) echo '{"id":2,"result":{}}' ;;
      *'"method":"thread/compact/start"'*)
        echo '{"id":3,"result":{}}'
        echo '{"method":"item/started","params":{"item":{"type":"contextCompaction","id":"c"}}}'
        echo '{"method":"error","params":{"message":"Reconnecting... 2/2"}}'
        echo '{"type":"event_msg","payload":{"type":"token_count","info":{"last_token_usage":{"total_tokens":80000},"model_context_window":475000}}}' >> "$ROLLOUT"
        echo '{"method":"item/completed","params":{"item":{"type":"contextCompaction","id":"c"}}}'
        echo '{"method":"turn/completed","params":{"turn":{"status":"completed"}}}' ;;
    esac
  done
  exit 0
fi
/bin/cat >/dev/null
echo '{"type":"thread.started","thread_id":"t-stream"}'
echo '{"type":"error","message":"Reconnecting... 2/2 (stream disconnected before completion: websocket closed by server before response.completed)"}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"answer"}}'
echo '{"type":"turn.completed"}'
`

func TestStreamWaitsForCompactionWithNotice(t *testing.T) {
	withSessionState(t)
	dir := fakeCodex(t, codexWithAppServer)
	t.Setenv("ROLLOUT", writeContextRollout(t, "t-stream", 400000, 475000))
	rec := httptest.NewRecorder()
	input := codexInput{IsResume: true, Args: []string{"exec", "resume", "t-stream", "--json"}}
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), input, "r", 1, "codex/gpt-6-astra", false, dir, nil, "s", nil)
	body := rec.Body.String()
	notice := strings.Index(body, "正在压缩上下文")
	answer := strings.Index(body, `"answer"`)
	if notice < 0 || answer < notice {
		t.Fatalf("want the notice before the answer:\n%s", body)
	}
	if strings.Contains(body, "[codex error]") || !strings.Contains(body, `"finish_reason":"stop"`) {
		t.Fatalf("a retry notice is not a failure:\n%s", body)
	}
	// The rollout now reports a compacted context: nothing runs after the turn.
	if job := ensureCompaction("t-stream", dir, ""); job != nil {
		t.Fatal("compacted thread must not compact again")
	}
}

func TestNonStreamIgnoresRetryNotice(t *testing.T) {
	withSessionState(t)
	dir := fakeCodex(t, codexWithAppServer)
	rec := httptest.NewRecorder()
	handleNonStreamResponse(rec, httptest.NewRequest("POST", "/", nil), codexInput{}, "r", 1, "m", dir, nil, "s", nil)
	if rec.Code != 200 || !strings.Contains(rec.Body.String(), "answer") {
		t.Fatal(rec.Code, rec.Body.String())
	}
}

func TestAppServerCompactionReportsTurnWithoutCompaction(t *testing.T) {
	fakeCodex(t, `while read line; do
  case "$line" in
    *'"method":"initialize"'*) echo '{"id":1,"result":{}}' ;;
    *'"method":"thread/resume"'*) echo '{"id":2,"result":{}}' ;;
    *'"method":"thread/compact/start"'*) echo '{"id":3,"result":{}}'; echo '{"method":"turn/completed","params":{"turn":{"status":"failed"}}}' ;;
  esac
done
`)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	err := compactViaAppServer(ctx, "t", "", t.TempDir(), "")
	if err == nil || !strings.Contains(err.Error(), "without a compaction") {
		t.Fatal(err)
	}
}

func TestAppServerCompactionReportsRequestError(t *testing.T) {
	fakeCodex(t, `while read line; do
  case "$line" in
    *'"method":"initialize"'*) echo '{"id":1,"result":{}}' ;;
    *'"method":"thread/resume"'*) echo '{"id":2,"error":{"code":-32600,"message":"thread not found"}}' ;;
  esac
done
`)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := compactViaAppServer(ctx, "t", "", t.TempDir(), ""); err == nil || !strings.Contains(err.Error(), "thread not found") {
		t.Fatal(err)
	}
}

func TestIsCodexRetryNotice(t *testing.T) {
	if !isCodexRetryNotice("Reconnecting... 5/5 (stream disconnected before completion: Broken pipe)") {
		t.Fatal("retry notice")
	}
	if isCodexRetryNotice("You’ve hit your usage limit.") {
		t.Fatal("real failure")
	}
}

func TestFailedCompactionIsNotRetriedRightAway(t *testing.T) {
	old := runCompaction
	runCompaction = func(ctx context.Context, threadID, path, workingDir, model string) error {
		return context.DeadlineExceeded
	}
	t.Cleanup(func() { runCompaction = old })
	writeContextRollout(t, "t-fail", 400000, 475000)
	job := ensureCompaction("t-fail", "", "")
	if job == nil {
		t.Fatal("must try once")
	}
	<-job.done
	if ensureCompaction("t-fail", "", "") != nil {
		t.Fatal("a failed thread must not make the next turn wait again")
	}
}
