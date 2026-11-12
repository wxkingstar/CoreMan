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
	return &openai.ChatCompletionRequest{SessionID: "topic", ForkSessionID: "main", ResumeFallback: "[group context]", Messages: []openai.ChatMessage{
		{Role: "user", Content: json.RawMessage(`"hi"`)},
	}}
}

func TestForkedInputForksBoundSession(t *testing.T) {
	withSessionState(t)
	if _, ok := forkedInput(forkRequest(), "m", "", ""); ok {
		t.Fatal("no source binding: nothing to fork")
	}
	if err := bindings.Set("main", "ses_main"); err != nil {
		t.Fatal(err)
	}
	input, ok := forkedInput(forkRequest(), "m", "", "")
	if !ok || !input.IsResume || input.Session != "ses_main" {
		t.Fatalf("ok=%v input=%+v", ok, input)
	}
	args := strings.Join(input.Args, " ")
	if !strings.Contains(args, "--session ses_main") || !strings.HasSuffix(args, "--fork") {
		t.Errorf("args = %v", input.Args)
	}
}

// The fork's events carry the new opencode session, which binds to the topic;
// the main session keeps its own.
func TestForkBindsNewSession(t *testing.T) {
	dir := fakeOpencode(t, `echo "$*" > "$(dirname "$0")/args.txt"
/bin/cat >/dev/null
echo '{"type":"step_start","sessionID":"ses_fork","part":{"type":"step-start"}}'
echo '{"type":"text","sessionID":"ses_fork","part":{"type":"text","text":"from the group"}}'
echo '{"type":"step_finish","sessionID":"ses_fork","part":{"reason":"stop","tokens":{"input":1,"output":1,"cache":{}}}}'
`)
	withSessionState(t)
	if err := bindings.Set("main", "ses_main"); err != nil {
		t.Fatal(err)
	}
	req := forkRequest()
	input, _ := forkedInput(req, "m", "", "")
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), input, "r", 1, "m", false, "", nil, "topic", newRebuildFresh(bindings, req, "m", "", ""))
	if !strings.Contains(rec.Body.String(), "from the group") {
		t.Fatal(rec.Body.String())
	}
	if got := bindings.Get("topic"); got != "ses_fork" {
		t.Errorf("topic binding = %q", got)
	}
	if got := bindings.Get("main"); got != "ses_main" {
		t.Errorf("main binding = %q", got)
	}
	data, _ := os.ReadFile(filepath.Join(dir, "args.txt"))
	if !strings.Contains(string(data), "--session ses_main") || !strings.Contains(string(data), "--fork") {
		t.Errorf("opencode args = %q", data)
	}
}

// The source session is gone: start the topic fresh with the continuation.
func TestForkMissingSourceStartsFresh(t *testing.T) {
	dir := fakeOpencode(t, `for a in "$@"; do
  if [ "$a" = --session ]; then printf '\033[91m\033[1mError: \033[0mSession not found\n' >&2; exit 1; fi
done
/bin/cat > "$(dirname "$0")/stdin.txt"
echo '{"type":"step_start","sessionID":"ses_new","part":{"type":"step-start"}}'
echo '{"type":"text","sessionID":"ses_new","part":{"type":"text","text":"fresh"}}'
echo '{"type":"step_finish","sessionID":"ses_new","part":{"reason":"stop","tokens":{"input":1,"output":1,"cache":{}}}}'
`)
	withSessionState(t)
	if err := bindings.Set("main", "ses_gone"); err != nil {
		t.Fatal(err)
	}
	req := forkRequest()
	input, _ := forkedInput(req, "m", "", "")
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), input, "r", 1, "m", false, "", nil, "topic", newRebuildFresh(bindings, req, "m", "", ""))
	if !strings.Contains(rec.Body.String(), "fresh") || bindings.Get("topic") != "ses_new" {
		t.Fatal(rec.Body.String())
	}
	if got := bindings.Get("main"); got != "ses_gone" {
		t.Errorf("main binding = %q", got)
	}
	data, _ := os.ReadFile(filepath.Join(dir, "stdin.txt"))
	if !strings.Contains(string(data), "[group context]") {
		t.Errorf("fresh run stdin = %q", data)
	}
}
