package main

import (
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"clawrelay-api/pkg/openai"
)

func forkRequest() *openai.ChatCompletionRequest {
	return &openai.ChatCompletionRequest{
		SessionID:      "topic",
		ForkSessionID:  "main",
		Messages:       []openai.ChatMessage{{Role: "user", Content: json.RawMessage(`"[SYS_TURN:t]\n- speaker\n[/SYS_TURN:t]\n\nhi"`)}},
		ResumeFallback: "- carried",
	}
}

func TestForkedInputForksSourceThread(t *testing.T) {
	withSessionState(t)
	if _, ok := forkedInput(forkRequest(), "m", ""); ok {
		t.Fatal("no source thread: nothing to fork")
	}
	if err := threads.Set("main", "main-thread"); err != nil {
		t.Fatal(err)
	}
	input, ok := forkedInput(forkRequest(), "m", "")
	if !ok || !input.IsResume {
		t.Fatalf("ok=%v resume=%v", ok, input.IsResume)
	}
	if strings.Join(input.Args[:3], " ") != "exec fork main-thread" {
		t.Errorf("args = %v", input.Args)
	}
	if strings.Contains(input.Stdin, "carried") {
		t.Errorf("a fork carries the history, not the continuation: %q", input.Stdin)
	}
	same := forkRequest()
	same.ForkSessionID = "topic"
	if _, ok := forkedInput(same, "m", ""); ok {
		t.Error("a session must not fork itself")
	}
}

// The fork's new thread binds to the topic session; the main session keeps its own.
func TestForkBindsNewThread(t *testing.T) {
	argsLog := filepath.Join(t.TempDir(), "args.log")
	t.Setenv("ARGSLOG", argsLog)
	dir := fakeCodex(t, `echo "$*" >> "$ARGSLOG"
/bin/cat >/dev/null
echo '{"type":"thread.started","thread_id":"forked-thread"}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"from the group"}}'
echo '{"type":"turn.completed"}'
`)
	withSessionState(t)
	if err := threads.Set("main", "main-thread"); err != nil {
		t.Fatal(err)
	}
	req := forkRequest()
	input, _ := forkedInput(req, "m", "")
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), input, "r", 1, "m", false, dir, nil, "topic", newRebuildFresh(threads, req, "m", ""))
	if !strings.Contains(rec.Body.String(), "from the group") {
		t.Fatalf("body:\n%s", rec.Body.String())
	}
	if got := threads.Get("topic"); got != "forked-thread" {
		t.Errorf("topic thread = %q", got)
	}
	if got := threads.Get("main"); got != "main-thread" {
		t.Errorf("main thread = %q", got)
	}
	data, _ := os.ReadFile(argsLog)
	if !strings.HasPrefix(string(data), "exec fork main-thread ") {
		t.Errorf("codex args = %q", data)
	}
}

// The source rollout is gone: start the topic fresh with the continuation.
func TestForkMissingSourceStartsFresh(t *testing.T) {
	stdinLog := filepath.Join(t.TempDir(), "stdin.log")
	t.Setenv("STDINLOG", stdinLog)
	dir := fakeCodex(t, `for a in "$@"; do
  if [ "$a" = fork ]; then /bin/cat > /dev/null; echo 'Error: thread/fork: thread/fork failed: no rollout found for thread id main-thread (code -32600)' >&2; exit 1; fi
done
/bin/cat >> "$STDINLOG"
echo '{"type":"thread.started","thread_id":"fresh-thread"}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"fresh"}}'
echo '{"type":"turn.completed"}'
`)
	withSessionState(t)
	if err := threads.Set("main", "main-thread"); err != nil {
		t.Fatal(err)
	}
	req := forkRequest()
	input, _ := forkedInput(req, "m", "")
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), input, "r", 1, "m", false, dir, nil, "topic", newRebuildFresh(threads, req, "m", ""))
	if !strings.Contains(rec.Body.String(), "fresh") || threads.Get("topic") != "fresh-thread" {
		t.Fatalf("body:\n%s", rec.Body.String())
	}
	data, _ := os.ReadFile(stdinLog)
	if !strings.Contains(string(data), "- speaker\n- carried\n[/SYS_TURN:t]") {
		t.Errorf("fresh run stdin = %q", data)
	}
}

// A fork's cumulative counter carries the source thread's totals: the topic
// starts from the source baseline so its first turn bills only itself.
func TestUsageMeterInheritsForkBaseline(t *testing.T) {
	m := newUsageMeter(t.TempDir())
	m.perTurn("main", codexUsage{InputTokens: 500, CachedInputTokens: 100, OutputTokens: 50})
	m.Inherit("topic", "main")
	in, out, cache := m.perTurn("topic", codexUsage{InputTokens: 800, CachedInputTokens: 300, OutputTokens: 70})
	if in != 100 || cache != 200 || out != 20 {
		t.Errorf("first forked turn = (%d, %d, %d), want (100, 20, 200)", in, out, cache)
	}
	m.Inherit("other", "missing")
	if in, _, _ := m.perTurn("other", codexUsage{InputTokens: 10}); in != 10 {
		t.Errorf("no source baseline: turn = %d, want 10", in)
	}
}
