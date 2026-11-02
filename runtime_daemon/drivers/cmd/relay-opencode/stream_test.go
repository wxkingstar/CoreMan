package main

import (
	"encoding/json"
	"net/http/httptest"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"clawrelay-api/pkg/openai"
	"clawrelay-api/pkg/sessions"
)

func fakeOpencode(t *testing.T, script string) string {
	t.Helper()
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "opencode"), []byte("#!/bin/sh\n"+script), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir+":/bin:/usr/bin")
	return dir
}

func withSessionState(t *testing.T) {
	t.Helper()
	oldBindings, oldStore := bindings, sessionStore
	bindings = newSessionMap(t.TempDir())
	sessionStore = sessions.New(t.TempDir())
	t.Cleanup(func() { bindings, sessionStore = oldBindings, oldStore })
}

// chunks decodes the SSE data frames of a recorded stream.
func chunks(t *testing.T, body string) []openai.ChatCompletionResponse {
	t.Helper()
	var out []openai.ChatCompletionResponse
	for _, line := range strings.Split(body, "\n") {
		if !strings.HasPrefix(line, "data: {") {
			continue
		}
		var c openai.ChatCompletionResponse
		if err := json.Unmarshal([]byte(strings.TrimPrefix(line, "data: ")), &c); err != nil {
			t.Fatal(err)
		}
		out = append(out, c)
	}
	return out
}

// Frames recorded from opencode 1.19 `run --format json`, trimmed.
const recordedTurn = `/bin/cat >/dev/null
cat <<'FRAMES'
{"type":"step_start","sessionID":"ses_a","part":{"type":"step-start"}}
{"type":"reasoning","sessionID":"ses_a","part":{"type":"reasoning","text":"plan the listing"}}
{"type":"tool_use","sessionID":"ses_a","part":{"type":"tool","tool":"bash","callID":"call_1","id":"prt_1","state":{"status":"completed","input":{"command":"ls"},"output":"a.txt\n"}}}
{"type":"step_finish","sessionID":"ses_a","part":{"reason":"tool-calls","type":"step-finish","tokens":{"total":4931,"input":3053,"output":38,"reasoning":48,"cache":{"write":0,"read":1792}},"cost":0}}
{"type":"step_start","sessionID":"ses_a","part":{"type":"step-start"}}
{"type":"tool_use","sessionID":"ses_a","part":{"type":"tool","tool":"bash","callID":"call_2","state":{"status":"error","input":{"command":"rm -f a.txt"},"error":"denied by rule"}}}
{"type":"text","sessionID":"ses_a","part":{"type":"text","text":"The directory contains a.txt."}}
{"type":"step_finish","sessionID":"ses_a","part":{"reason":"stop","type":"step-finish","tokens":{"total":5168,"input":162,"output":14,"reasoning":0,"cache":{"write":10,"read":4992}},"cost":0.5}}
FRAMES
`

func TestStreamTranslatesRecordedTurn(t *testing.T) {
	fakeOpencode(t, recordedTurn)
	withSessionState(t)
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), opencodeInput{}, "r", 1, "opencode/opencode-go/kimi-k3", true, "", nil, "client-1", nil)

	var text, thinking strings.Builder
	var tools []string
	var finish string
	var usage *openai.UsageInfo
	for _, c := range chunks(t, rec.Body.String()) {
		if c.Usage != nil {
			usage = c.Usage
		}
		for _, choice := range c.Choices {
			if choice.FinishReason != nil {
				finish = *choice.FinishReason
			}
			if choice.Delta == nil {
				continue
			}
			text.WriteString(choice.Delta.ContentString())
			thinking.WriteString(choice.Delta.Thinking)
			for _, call := range choice.Delta.ToolCalls {
				tools = append(tools, call.ID+":"+call.Function.Name+":"+call.Function.Arguments)
			}
		}
	}
	if text.String() != "The directory contains a.txt." || finish != "stop" {
		t.Fatalf("text=%q finish=%q", text.String(), finish)
	}
	if strings.Join(tools, "|") != `call_1:bash:{"command":"ls"}|call_2:bash:{"command":"rm -f a.txt"}` {
		t.Fatalf("tools=%v", tools)
	}
	for _, want := range []string{"plan the listing", "工具结果 · bash\na.txt", "error: denied by rule"} {
		if !strings.Contains(thinking.String(), want) {
			t.Errorf("thinking missing %q: %s", want, thinking.String())
		}
	}
	// Steps add up; reasoning counts as output; cache writes are reported.
	if usage == nil || usage.CompletionTokens != 38+48+14 || usage.PromptTokens != 3053+1792+162+4992+10 {
		t.Fatalf("usage=%+v", usage)
	}
	if usage.PromptTokensDetails == nil || usage.PromptTokensDetails.CachedTokens != 1792+4992 || usage.PromptTokensDetails.CacheCreationTokens != 10 {
		t.Fatalf("details=%+v", usage.PromptTokensDetails)
	}
	if got := bindings.Get("client-1"); got != "ses_a" {
		t.Fatalf("binding=%q", got)
	}
}

func TestStreamErrorEventFailsTurn(t *testing.T) {
	fakeOpencode(t, `/bin/cat >/dev/null
echo '{"type":"error","sessionID":"ses_b","error":{"name":"APIError","data":{"message":"Go usage limit reached"}}}'
exit 1
`)
	withSessionState(t)
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), opencodeInput{}, "r", 1, "m", false, "", nil, "s", nil)
	body := rec.Body.String()
	if !strings.Contains(body, "[opencode error] Go usage limit reached") || !strings.Contains(body, `"finish_reason":"error"`) {
		t.Fatal(body)
	}
}

// Verified: a model call failure is only logged on stderr; the run then
// neither prints an event nor exits.
func TestSilentModelFailureEndsTurnAfterGrace(t *testing.T) {
	old := apiErrorGrace
	apiErrorGrace = 200 * time.Millisecond
	t.Cleanup(func() { apiErrorGrace = old })
	fakeOpencode(t, `/bin/cat >/dev/null
echo 'timestamp=2026-11-01T16:56:53.493Z level=ERROR run=e8 message="stream error" providerID=opencode error.error="AI_APICallError: Free usage limit reached. Subscribe to keep going"' >&2
exec /bin/sleep 30
`)
	withSessionState(t)
	rec := httptest.NewRecorder()
	start := time.Now()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), opencodeInput{}, "r", 1, "m", false, "", nil, "s", nil)
	if time.Since(start) > 10*time.Second {
		t.Fatal("run was not stopped")
	}
	body := rec.Body.String()
	if !strings.Contains(body, "[opencode error] Free usage limit reached. Subscribe to keep going") || !strings.Contains(body, "data: [DONE]") {
		t.Fatal(body)
	}
}

func TestSilentFailureAfterProgressStillEnds(t *testing.T) {
	old := apiErrorGrace
	apiErrorGrace = 200 * time.Millisecond
	t.Cleanup(func() { apiErrorGrace = old })
	fakeOpencode(t, `/bin/cat >/dev/null
echo '{"type":"step_start","sessionID":"ses_c","part":{"type":"step-start"}}'
/bin/sleep 0.2
echo 'level=ERROR message="stream error" error.error="AI_APICallError: overloaded"' >&2
exec /bin/sleep 30
`)
	withSessionState(t)
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), opencodeInput{}, "r", 1, "m", false, "", nil, "s", nil)
	if body := rec.Body.String(); !strings.Contains(body, "[opencode error] overloaded") || !strings.Contains(body, `"finish_reason":"error"`) {
		t.Fatal(body)
	}
}

func TestMissingSessionStartsFreshWithFallback(t *testing.T) {
	fakeOpencode(t, `for a in "$@"; do
  if [ "$a" = --session ]; then printf '\033[91m\033[1mError: \033[0mSession not found\n' >&2; exit 1; fi
done
/bin/cat > "$(dirname "$0")/stdin.txt"
echo '{"type":"step_start","sessionID":"ses_new","part":{"type":"step-start"}}'
echo '{"type":"text","sessionID":"ses_new","part":{"type":"text","text":"hello again"}}'
echo '{"type":"step_finish","sessionID":"ses_new","part":{"reason":"stop","tokens":{"input":1,"output":1,"cache":{}}}}'
`)
	withSessionState(t)
	if err := bindings.Set("client", "ses_dead"); err != nil {
		t.Fatal(err)
	}
	req := &openai.ChatCompletionRequest{SessionID: "client", ResumeFallback: "[earlier context]", Messages: []openai.ChatMessage{
		{Role: "user", Content: json.RawMessage(`"hi"`)},
	}}
	input := buildInput(req, "m", bindings.Get("client"), "", "")
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), input, "r", 1, "m", false, "", nil, "client", newRebuildFresh(bindings, req, "m", "", ""))
	body := rec.Body.String()
	if !strings.Contains(body, "之前的会话记录已不存在") || !strings.Contains(body, "hello again") {
		t.Fatal(body)
	}
	if got := bindings.Get("client"); got != "ses_new" {
		t.Fatalf("binding=%q", got)
	}
}

func TestZeroOutputOtherFailureKeepsSession(t *testing.T) {
	dir := fakeOpencode(t, "echo 'Error: Failed to initialize provider' >&2\nexit 1\n")
	withSessionState(t)
	rebuilt := false
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), opencodeInput{IsResume: true}, "r", 1, "m", false, dir, nil, "existing", func() opencodeInput { rebuilt = true; return opencodeInput{} })
	if rebuilt {
		t.Fatal("must not drop the session on other failures")
	}
	body := rec.Body.String()
	errAt := strings.Index(body, "[opencode error] opencode produced no output: Failed to initialize provider")
	finishAt := strings.Index(body, `"finish_reason":"stop"`)
	doneAt := strings.Index(body, "data: [DONE]")
	if !strings.Contains(body, `"x_relay_error":true`) || errAt < 0 || finishAt < errAt || doneAt < finishAt {
		t.Fatal(body)
	}
}

func TestExitWithoutFinishIsIncomplete(t *testing.T) {
	fakeOpencode(t, `/bin/cat >/dev/null
echo '{"type":"text","sessionID":"ses_d","part":{"type":"text","text":"partial"}}'
`)
	withSessionState(t)
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), opencodeInput{}, "r", 1, "m", false, "", nil, "s", nil)
	if body := rec.Body.String(); !strings.Contains(body, "incomplete_result") || !strings.Contains(body, `"finish_reason":"error"`) {
		t.Fatal(body)
	}
}

func TestNonStreamReturnsText(t *testing.T) {
	fakeOpencode(t, recordedTurn)
	withSessionState(t)
	rec := httptest.NewRecorder()
	handleNonStreamResponse(rec, httptest.NewRequest("POST", "/", nil), opencodeInput{}, "r", 1, "m", "", nil, "s", nil)
	var resp openai.ChatCompletionResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(rec.Body.String())
	}
	if resp.Choices[0].Message.ContentString() != "The directory contains a.txt." || resp.Usage == nil {
		t.Fatalf("%+v", resp)
	}
}

func TestNonStreamSilentFailure(t *testing.T) {
	old := apiErrorGrace
	apiErrorGrace = 200 * time.Millisecond
	t.Cleanup(func() { apiErrorGrace = old })
	fakeOpencode(t, `/bin/cat >/dev/null
echo 'level=ERROR message="stream error" error.error="AI_APICallError: usage limit reached"' >&2
exec /bin/sleep 30
`)
	withSessionState(t)
	rec := httptest.NewRecorder()
	handleNonStreamResponse(rec, httptest.NewRequest("POST", "/", nil), opencodeInput{}, "r", 1, "m", "", nil, "s", nil)
	if rec.Code != 500 || !strings.Contains(rec.Body.String(), "usage limit reached") {
		t.Fatal(rec.Code, rec.Body.String())
	}
}

func TestPromptGoesThroughStdinAndFilesAfterIt(t *testing.T) {
	dir := fakeOpencode(t, `printf '%s\n' "$@" > "$(dirname "$0")/args.txt"
/bin/cat > "$(dirname "$0")/stdin.txt"
echo '{"type":"step_finish","sessionID":"ses_e","part":{"reason":"stop","tokens":{"input":1,"output":1,"cache":{}}}}'
`)
	withSessionState(t)
	upload := t.TempDir()
	png := "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
	req := &openai.ChatCompletionRequest{Effort: "high", Messages: []openai.ChatMessage{
		{Role: "system", Content: json.RawMessage(`"rules"`)},
		{Role: "user", Content: json.RawMessage(`[{"type":"text","text":"line one\nline two"},{"type":"image_url","image_url":{"url":"data:image/png;base64,` + png + `"}}]`)},
	}}
	input := buildInput(req, "opencode/opencode-go/glm-5.3", "ses_old", upload, "/rules.md")
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), input, "r", 1, "m", false, "", nil, "s", nil)
	args, _ := os.ReadFile(filepath.Join(dir, "args.txt"))
	stdin, _ := os.ReadFile(filepath.Join(dir, "stdin.txt"))
	got := strings.Split(strings.TrimSpace(string(args)), "\n")
	want := []string{"run", "--format", "json", "--print-logs", "--log-level", "WARN", "--session", "ses_old", "--model", "opencode-go/glm-5.3", "--variant", "high", "--file"}
	if len(got) != len(want)+1 || strings.Join(got[:len(want)], " ") != strings.Join(want, " ") || !strings.HasPrefix(got[len(want)], upload) {
		t.Fatalf("args=%q", got)
	}
	if string(stdin) != "line one\nline two" {
		t.Fatalf("stdin=%q", stdin)
	}
}
