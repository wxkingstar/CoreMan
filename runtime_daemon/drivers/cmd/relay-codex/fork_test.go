package main

import (
	"context"
	"encoding/json"
	"errors"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"clawrelay-api/pkg/openai"
	"clawrelay-api/pkg/proc"
)

func forkRequest() *openai.ChatCompletionRequest {
	return &openai.ChatCompletionRequest{
		SessionID:      "topic",
		ForkSessionID:  "main",
		Messages:       []openai.ChatMessage{{Role: "user", Content: json.RawMessage(`"[SYS_TURN:t]\n- speaker\n[/SYS_TURN:t]\n\nhi"`)}},
		ResumeFallback: "- carried",
	}
}

type forkCall struct {
	thread string
	busy   bool
}

// stubFork replaces the app-server fork; it records each call.
func stubFork(t *testing.T, thread string, err error) *[]forkCall {
	t.Helper()
	calls := &[]forkCall{}
	old := forkThreadVia
	forkThreadVia = func(_ context.Context, threadID, _ string, busy bool) (string, error) {
		*calls = append(*calls, forkCall{threadID, busy})
		return thread, err
	}
	t.Cleanup(func() { forkThreadVia = old })
	return calls
}

func TestForkedInputResumesTheForkedThread(t *testing.T) {
	withSessionState(t)
	calls := stubFork(t, "forked-thread", nil)
	if _, ok := forkedInput(forkRequest(), "m", ""); ok || len(*calls) != 0 {
		t.Fatal("no source thread: nothing to fork")
	}
	if err := threads.Set("main", "main-thread"); err != nil {
		t.Fatal(err)
	}
	input, ok := forkedInput(forkRequest(), "m", "")
	if !ok || !input.IsResume || strings.Join(input.Args[:3], " ") != "exec resume forked-thread" {
		t.Fatalf("ok=%v args=%v", ok, input.Args)
	}
	if got := threads.Get("topic"); got != "forked-thread" {
		t.Errorf("topic thread = %q", got)
	}
	if got := threads.Get("main"); got != "main-thread" {
		t.Errorf("main thread = %q", got)
	}
	if (*calls)[0] != (forkCall{"main-thread", false}) {
		t.Errorf("fork call = %+v", (*calls)[0])
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

// A turn running in the main session is left out of the fork.
func TestBusySourceForksBeforeTheRunningTurn(t *testing.T) {
	withSessionState(t)
	calls := stubFork(t, "forked-thread", nil)
	if err := threads.Set("main", "main-thread"); err != nil {
		t.Fatal(err)
	}
	release, err := proc.AcquireSession(context.Background(), "main")
	if err != nil {
		t.Fatal(err)
	}
	defer release()
	if _, ok := forkedInput(forkRequest(), "m", ""); !ok {
		t.Fatal("fork expected")
	}
	if !(*calls)[0].busy {
		t.Error("the main session is running a turn: the fork must leave it out")
	}
}

func TestAppServerFailureForksWithExec(t *testing.T) {
	withSessionState(t)
	stubFork(t, "", errors.New("unknown method thread/fork"))
	if err := threads.Set("main", "main-thread"); err != nil {
		t.Fatal(err)
	}
	input, ok := forkedInput(forkRequest(), "m", "")
	if !ok || strings.Join(input.Args[:3], " ") != "exec fork main-thread" {
		t.Fatalf("ok=%v args=%v", ok, input.Args)
	}
	if threads.Get("topic") != "" {
		t.Error("the exec fork binds its own thread when it starts")
	}
}

func TestSourceStillOnItsFirstTurnStartsFresh(t *testing.T) {
	withSessionState(t)
	stubFork(t, "", errNothingFinished)
	if err := threads.Set("main", "main-thread"); err != nil {
		t.Fatal(err)
	}
	if _, ok := forkedInput(forkRequest(), "m", ""); ok {
		t.Fatal("nothing finished to continue: start fresh")
	}
}

func TestForkPoint(t *testing.T) {
	running := []forkTurn{{"t3", "interrupted"}, {"t2", "completed"}}
	if last, ok := forkPoint(running, false); !ok || last != "" {
		t.Errorf("idle source: whole thread, got %q %v", last, ok)
	}
	if last, ok := forkPoint(running, true); !ok || last != "t2" {
		t.Errorf("busy source: before the running turn, got %q %v", last, ok)
	}
	if last, ok := forkPoint([]forkTurn{{"t3", "completed"}, {"t2", "completed"}}, true); !ok || last != "" {
		t.Errorf("busy source whose latest turn just finished: whole thread, got %q %v", last, ok)
	}
	if _, ok := forkPoint([]forkTurn{{"t1", "inProgress"}}, true); ok {
		t.Error("only the running turn: nothing to fork")
	}
}

// fakeAppServer answers the JSON-RPC calls a fork makes and logs the fork
// request. The test PATH holds only the fake, so tools are called by path.
const fakeAppServer = `[ "$1" = app-server ] || exit 2
while IFS= read -r line; do
  id=$(printf '%s' "$line" | /usr/bin/sed -n 's/^{"id":\([0-9]*\).*/\1/p')
  case "$line" in
  *'"method":"initialize"'*) echo "{\"id\":$id,\"result\":{}}" ;;
  *'"method":"thread/turns/list"'*)
    echo '{"method":"thread/status","params":{}}'
    echo "{\"id\":$id,\"result\":{\"data\":[{\"id\":\"t3\",\"status\":\"interrupted\"},{\"id\":\"t2\",\"status\":\"completed\"}]}}" ;;
  *'"method":"thread/fork"'*)
    printf '%s\n' "$line" >> "$FORKLOG"
    echo "{\"id\":$id,\"result\":{\"thread\":{\"id\":\"forked-thread\"}}}" ;;
  esac
done
`

func TestForkViaAppServerStopsBeforeTheRunningTurn(t *testing.T) {
	forkLog := filepath.Join(t.TempDir(), "fork.log")
	t.Setenv("FORKLOG", forkLog)
	fakeCodex(t, fakeAppServer)
	got, err := forkViaAppServer(context.Background(), "main-thread", "", true)
	if err != nil || got != "forked-thread" {
		t.Fatalf("got %q err %v", got, err)
	}
	if _, err := forkViaAppServer(context.Background(), "main-thread", "", false); err != nil {
		t.Fatal(err)
	}
	data, _ := os.ReadFile(forkLog)
	lines := strings.Split(strings.TrimSpace(string(data)), "\n")
	if len(lines) != 2 {
		t.Fatalf("fork requests = %q", lines)
	}
	var busy, idle struct {
		Params map[string]any `json:"params"`
	}
	_ = json.Unmarshal([]byte(lines[0]), &busy)
	_ = json.Unmarshal([]byte(lines[1]), &idle)
	if busy.Params["threadId"] != "main-thread" || busy.Params["lastTurnId"] != "t2" {
		t.Errorf("busy fork params = %v", busy.Params)
	}
	if _, ok := idle.Params["lastTurnId"]; ok {
		t.Errorf("idle fork params = %v", idle.Params)
	}
}

// The forked thread is resumed by the turn; the main session keeps its own.
func TestForkedTurnResumesTheNewThread(t *testing.T) {
	argsLog := filepath.Join(t.TempDir(), "args.log")
	t.Setenv("ARGSLOG", argsLog)
	dir := fakeCodex(t, `echo "$*" >> "$ARGSLOG"
/bin/cat >/dev/null
echo '{"type":"thread.started","thread_id":"forked-thread"}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"from the group"}}'
echo '{"type":"turn.completed"}'
`)
	withSessionState(t)
	stubFork(t, "forked-thread", nil)
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
	if threads.Get("topic") != "forked-thread" || threads.Get("main") != "main-thread" {
		t.Errorf("topic=%q main=%q", threads.Get("topic"), threads.Get("main"))
	}
	data, _ := os.ReadFile(argsLog)
	if !strings.HasPrefix(string(data), "exec resume forked-thread ") {
		t.Errorf("codex args = %q", data)
	}
}

// The app-server fork failed and the exec fork finds no source rollout: start
// the topic fresh with the continuation.
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
	stubFork(t, "", errors.New("no rollout found"))
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
