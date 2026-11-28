package main

import (
	"bytes"
	"context"
	"encoding/json"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"clawrelay-api/pkg/openai"
	"clawrelay-api/pkg/sessions"
)

// fakeAgy is a minimal ACP server. It records every request it receives in
// $FAKE_LOG and answers by $FAKE_MODE; the frames mirror agy 1.5.19.
const fakeAgy = `#!/usr/bin/env python3
import json, os, sys

if sys.argv[1:2] == ["--version"]:
    print("1.5.19"); sys.exit(0)
if sys.argv[1:2] == ["models"]:
    print("gemini-4-neon-high\tGemini 4 Neon (High)\ngemini-4-neon-low\tGemini 4 Neon (Low)\ngpt-oss-120b-medium\tGPT-OSS 120B (Medium)"); sys.exit(0)
mode = os.environ.get("FAKE_MODE", "ok")
log = open(os.environ["FAKE_LOG"], "a")

def out(msg):
    sys.stdout.write(json.dumps(msg) + "\n"); sys.stdout.flush()

def update(sid, body):
    out({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": sid, "update": body}})

for line in sys.stdin:
    msg = json.loads(line)
    log.write(json.dumps({"argv": sys.argv[1:], "msg": msg, "probe": os.environ.get("PROBE_VAR")}) + "\n"); log.flush()
    method, rid, p = msg.get("method"), msg.get("id"), msg.get("params") or {}
    ok = lambda result: out({"jsonrpc": "2.0", "id": rid, "result": result})
    err = lambda code, text: out({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": text}})
    if method == "initialize":
        ok({"protocolVersion": 1})
    elif method == "session/new":
        if mode == "auth":
            err(-32000, "Authentication required")
        else:
            ok({"sessionId": "aaaaaaaa-0000-4000-8000-000000000001"})
    elif method == "session/resume":
        if mode == "stale" or p["sessionId"].startswith("dead"):
            err(-32603, "Failed to read session %s; retry, or load it with session/load" % p["sessionId"])
        else:
            ok({})
    elif method == "session/set_config_option":
        if p["configId"] == "model" and p["value"] == "nope":
            err(-32602, "Unknown model ID: nope")
        else:
            update(p["sessionId"], {"sessionUpdate": "config_option_update", "configOptions": [{"id": p["configId"], "currentValue": p["value"]}]})
            ok({})
    elif method == "session/set_model":
        err(-32602, "Unknown model ID: " + p["modelId"])
    elif method == "session/prompt":
        sid = p["sessionId"]
        if mode == "cancel":
            update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "working"}})
            for line2 in sys.stdin:
                m2 = json.loads(line2)
                log.write(json.dumps({"msg": m2}) + "\n"); log.flush()
                if m2.get("method") == "session/cancel":
                    break
            ok({"stopReason": "cancelled", "usage": {"inputTokens": 5, "outputTokens": 1, "cachedReadTokens": 0, "cachedWriteTokens": 0}})
            continue
        if mode == "permission":
            out({"jsonrpc": "2.0", "id": "p1", "method": "session/request_permission", "params": {"sessionId": sid, "options": [
                {"optionId": "allow", "name": "Allow", "kind": "allow_once"}, {"optionId": "deny", "name": "Deny", "kind": "reject_once"}]}})
            answer = json.loads(sys.stdin.readline())
            log.write(json.dumps({"msg": answer}) + "\n"); log.flush()
        update(sid, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "plan the listing"}})
        call = {"toolCallId": "call_1", "name": "run_command", "kind": "execute", "title": "ls",
                "rawInput": {"CommandLine": "ls", "toolSummary": "List the files"}}
        update(sid, dict(call, sessionUpdate="tool_call", status="in_progress"))
        update(sid, dict(call, sessionUpdate="tool_call_update", status="completed", rawOutput={"output": "a.txt\n", "exit_code": 0}))
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "The directory "}})
        update(sid, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "contains a.txt."}})
        if mode == "exit":
            sys.exit(3)
        ok({"stopReason": "end_turn", "usage": {"inputTokens": 3053, "outputTokens": 86, "cachedReadTokens": 1792, "cachedWriteTokens": 0, "thoughtTokens": 48}})
`

// received is one request the fake CLI logged.
type received struct {
	Argv  []string `json:"argv"`
	Probe string   `json:"probe"`
	Msg   struct {
		ID     json.RawMessage `json:"id"`
		Method string          `json:"method"`
		Params json.RawMessage `json:"params"`
		Result json.RawMessage `json:"result"`
	} `json:"msg"`
}

type fake struct {
	t   *testing.T
	log string
}

func newFake(t *testing.T, mode string) *fake {
	t.Helper()
	if _, err := exec.LookPath("python3"); err != nil {
		t.Skip("python3 is required for the fake CLI")
	}
	dir := t.TempDir()
	path := filepath.Join(dir, "agy")
	if err := os.WriteFile(path, []byte(fakeAgy), 0700); err != nil {
		t.Fatal(err)
	}
	old := agyBinary
	agyBinary = path
	t.Cleanup(func() { agyBinary = old })
	f := &fake{t: t, log: filepath.Join(dir, "requests.jsonl")}
	t.Setenv("FAKE_LOG", f.log)
	t.Setenv("FAKE_MODE", mode)

	oldBindings, oldStore := bindings, sessionStore
	sessionStore = sessions.New(t.TempDir())
	bindings = newSessionMap(sessionStore.AbsDir())
	t.Cleanup(func() { bindings, sessionStore = oldBindings, oldStore })
	return f
}

func (f *fake) requests() []received {
	f.t.Helper()
	data, _ := os.ReadFile(f.log)
	var out []received
	for _, line := range bytes.Split(data, []byte("\n")) {
		if len(line) == 0 {
			continue
		}
		var r received
		if err := json.Unmarshal(line, &r); err != nil {
			f.t.Fatal(err)
		}
		out = append(out, r)
	}
	return out
}

// params returns the params of every request with the given method.
func (f *fake) params(method string) []map[string]any {
	var out []map[string]any
	for _, r := range f.requests() {
		if r.Msg.Method == method {
			var p map[string]any
			_ = json.Unmarshal(r.Msg.Params, &p)
			out = append(out, p)
		}
	}
	return out
}

// post sends a request through the handler and returns the recorded response.
func post(t *testing.T, body map[string]any) *httptest.ResponseRecorder {
	t.Helper()
	data, _ := json.Marshal(body)
	rec := httptest.NewRecorder()
	chatCompletionsHandler(rec, httptest.NewRequest("POST", "/v1/chat/completions", bytes.NewReader(data)))
	return rec
}

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

// summary flattens a stream for assertions.
type summary struct {
	text, thinking, finish, actual string
	tools                          []openai.ToolCall
	usage                          *openai.UsageInfo
	relayError                     bool
}

func summarize(t *testing.T, body string) summary {
	t.Helper()
	var s summary
	for _, c := range chunks(t, body) {
		if c.Usage != nil {
			s.usage = c.Usage
		}
		s.relayError = s.relayError || c.XRelayError
		for _, ch := range c.Choices {
			if ch.Delta != nil {
				s.text += ch.Delta.ContentString()
				s.thinking += ch.Delta.Thinking
				s.tools = append(s.tools, ch.Delta.ToolCalls...)
			}
			if ch.FinishReason != nil {
				s.finish, s.actual = *ch.FinishReason, c.XActualModel
			}
		}
	}
	return s
}

func user(text string) []map[string]any {
	return []map[string]any{{"role": "user", "content": text}}
}

func TestStreamTranslatesTurn(t *testing.T) {
	f := newFake(t, "ok")
	work := t.TempDir()
	rec := post(t, map[string]any{
		"model": "antigravity/gemini-4-neon", "effort": "max", "stream": true, "stream_options": map[string]bool{"include_usage": true},
		"session_id": "s1", "working_dir": work, "add_dirs": []string{"/extra"},
		"env_vars": map[string]string{"PROBE_VAR": "from-bot", "AGY_ADC_AUTH": "true", "COREMAN_COLLABORATION_URL": "https://c.example.com/mcp", "COREMAN_COLLABORATION_TOKEN": "tok-1"},
		"messages": []map[string]any{{"role": "system", "content": "Be brief."}, {"role": "user", "content": "list files"}},
	})
	s := summarize(t, rec.Body.String())
	if s.text != "The directory contains a.txt." || s.finish != "stop" {
		t.Fatalf("text=%q finish=%q body=%s", s.text, s.finish, rec.Body.String())
	}
	if !strings.Contains(s.thinking, "plan the listing") || !strings.Contains(s.thinking, "工具结果 · run_command\na.txt") {
		t.Fatalf("thinking=%q", s.thinking)
	}
	if len(s.tools) != 1 || s.tools[0].Function.Name != "run_command" || s.tools[0].Summary != "List the files" {
		t.Fatalf("tools=%+v", s.tools)
	}
	// inputTokens excludes cached reads; OpenAI's prompt_tokens includes them.
	if s.usage == nil || s.usage.PromptTokens != 3053+1792 || s.usage.CompletionTokens != 86 {
		t.Fatalf("usage=%+v", s.usage)
	}
	if s.actual != "antigravity/gemini-4-neon" {
		t.Fatalf("actual model=%q", s.actual)
	}
	if !strings.HasSuffix(strings.TrimSpace(rec.Body.String()), "data: [DONE]") {
		t.Fatal("stream must end with [DONE]")
	}

	if got := bindings.Get("s1"); got != "aaaaaaaa-0000-4000-8000-000000000001" {
		t.Fatalf("binding=%q", got)
	}
	created := f.params("session/new")
	if len(created) != 1 {
		t.Fatalf("session/new calls=%d", len(created))
	}
	p := created[0]
	if p["cwd"] != work {
		t.Fatalf("cwd=%v", p["cwd"])
	}
	// The rules ride on the new conversation's system prompt, not on disk.
	meta := p["_meta"].(map[string]any)["antigravity"].(map[string]any)["systemPrompt"].(map[string]any)
	if meta["append"] != "Be brief." {
		t.Fatalf("append=%v", meta["append"])
	}
	rules := sessionRulesDir("s1")
	if _, err := os.Stat(filepath.Join(rules, rulesFile)); !os.IsNotExist(err) {
		t.Fatal("a new conversation must not carry a rule file")
	}
	dirs := p["additionalDirectories"].([]any)
	if len(dirs) != 2 || dirs[0] != rules || dirs[1] != "/extra" {
		t.Fatalf("additionalDirectories=%v", dirs)
	}
	servers := p["mcpServers"].([]any)
	if len(servers) != 1 {
		t.Fatalf("mcpServers=%v", servers)
	}
	server := servers[0].(map[string]any)
	header := server["headers"].([]any)[0].(map[string]any)
	if server["name"] != "coreman_collaboration" || server["type"] != "http" || server["url"] != "https://c.example.com/mcp" || header["value"] != "Bearer tok-1" {
		t.Fatalf("server=%v", server)
	}
	options := map[string]any{}
	for _, o := range f.params("session/set_config_option") {
		options[o["configId"].(string)] = o["value"]
	}
	if options["model"] != "gemini-4-neon" || options["thought_level"] != "xhigh" {
		t.Fatalf("options=%v", options)
	}
	if _, set := options["mode"]; set {
		t.Fatal("the default permission mode must not set a session mode")
	}
	first := f.requests()[0]
	if len(first.Argv) != 2 || first.Argv[0] != "acp" || first.Argv[1] != "--dangerously-skip-permissions" {
		t.Fatalf("argv=%v", first.Argv)
	}
	// The bot's variables reach the CLI; ones that redirect the CLI do not.
	if first.Probe != "from-bot" {
		t.Fatalf("PROBE_VAR=%q", first.Probe)
	}
	// Only names are reported: the process environment may hold real secrets.
	for _, e := range cleanEnv(map[string]string{"AGY_ADC_AUTH": "from-bot", "GOOGLE_GEMINI_BASE_URL": "from-bot", "HOME": "/from-bot", "OK": "1"}) {
		if name, value, _ := strings.Cut(e, "="); strings.Contains(value, "from-bot") {
			t.Fatalf("the bot must not set %s", name)
		}
	}
	prompt := f.params("session/prompt")[0]["prompt"].([]any)[0].(map[string]any)
	if prompt["text"] != "list files" {
		t.Fatalf("prompt=%v", prompt)
	}
}

func TestResumeSendsOnlyLatestMessageAndChangedRules(t *testing.T) {
	f := newFake(t, "ok")
	first := map[string]any{"stream": true, "session_id": "s1", "working_dir": t.TempDir(),
		"messages": []map[string]any{{"role": "system", "content": "Rules v1."}, {"role": "user", "content": "one"}}}
	post(t, first)

	// Same rules: the conversation already has them, nothing is staged.
	first["messages"] = []map[string]any{{"role": "system", "content": "Rules v1."}, {"role": "user", "content": "one"}, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "two"}}
	post(t, first)
	rules := sessionRulesDir("s1")
	if _, err := os.Stat(filepath.Join(rules, rulesFile)); !os.IsNotExist(err) {
		t.Fatal("unchanged rules must not be staged again")
	}
	resumed := f.params("session/resume")
	if len(resumed) != 1 || resumed[0]["sessionId"] != "aaaaaaaa-0000-4000-8000-000000000001" {
		t.Fatalf("resume=%v", resumed)
	}
	if _, has := resumed[0]["_meta"]; has {
		t.Fatal("a resumed conversation ignores an appended system prompt; do not send it")
	}
	prompts := f.params("session/prompt")
	if text := prompts[1]["prompt"].([]any)[0].(map[string]any)["text"]; text != "two" {
		t.Fatalf("resume prompt=%v", text)
	}

	// Changed rules that fit a rule file are staged inline.
	first["messages"] = []map[string]any{{"role": "system", "content": "Rules v2."}, {"role": "user", "content": "three"}}
	post(t, first)
	staged, _ := os.ReadFile(filepath.Join(rules, rulesFile))
	if !strings.HasSuffix(string(staged), "Rules v2.") || !strings.HasPrefix(string(staged), supersedeNote) {
		t.Fatalf("staged=%q", staged)
	}

	// Changed rules too large for a rule file: the full text goes to a plain
	// file and the prompt says to read it, once per version.
	big := strings.Repeat("policy line\n", 4000) + "Rules v3."
	first["messages"] = []map[string]any{{"role": "system", "content": big}, {"role": "user", "content": "four"}}
	post(t, first)
	post(t, first)
	prompts = f.params("session/prompt")
	fourth := prompts[3]["prompt"].([]any)[0].(map[string]any)["text"].(string)
	full := filepath.Join(rules, fullRulesFile)
	if !strings.HasPrefix(fourth, "[Platform notice:") || !strings.Contains(fourth, full) || !strings.HasSuffix(fourth, "four") {
		t.Fatalf("notice prompt=%q", fourth)
	}
	if fifth := prompts[4]["prompt"].([]any)[0].(map[string]any)["text"]; fifth != "four" {
		t.Fatalf("the same rules must be announced once, got %q", fifth)
	}
	if data, _ := os.ReadFile(full); string(data) != big {
		t.Fatal("full rules file must hold the current rules")
	}
	if _, err := os.Stat(filepath.Join(rules, rulesFile)); !os.IsNotExist(err) {
		t.Fatal("the truncated rule file must be removed when the rules are too large")
	}
}

func TestMissingConversationStartsFreshWithFallback(t *testing.T) {
	f := newFake(t, "ok")
	if err := bindings.Set("s1", "dead0000-0000-4000-8000-000000000000"); err != nil {
		t.Fatal(err)
	}
	rec := post(t, map[string]any{"stream": true, "session_id": "s1", "working_dir": t.TempDir(), "resume_fallback": "(carried context)",
		"messages": []map[string]any{{"role": "system", "content": "Rules."}, {"role": "user", "content": "hello"}}})
	s := summarize(t, rec.Body.String())
	if !strings.HasPrefix(s.text, staleNotice) || !strings.HasSuffix(s.text, "contains a.txt.") || s.finish != "stop" {
		t.Fatalf("text=%q finish=%q", s.text, s.finish)
	}
	if got := bindings.Get("s1"); got != "aaaaaaaa-0000-4000-8000-000000000001" {
		t.Fatalf("binding=%q", got)
	}
	created := f.params("session/new")
	if len(created) != 1 || created[0]["_meta"] == nil {
		t.Fatalf("the new conversation must take the rules as its system prompt: %v", created)
	}
	prompt := f.params("session/prompt")[0]["prompt"].([]any)[0].(map[string]any)["text"].(string)
	if !strings.Contains(prompt, "(carried context)") || !strings.Contains(prompt, "hello") {
		t.Fatalf("prompt=%q", prompt)
	}
}

func TestNotLoggedInFailsVisibly(t *testing.T) {
	newFake(t, "auth")
	rec := post(t, map[string]any{"stream": true, "session_id": "s1", "working_dir": t.TempDir(), "messages": user("hi")})
	s := summarize(t, rec.Body.String())
	if s.finish != "error" || !strings.Contains(s.text, errorMarker) || !strings.Contains(s.text, "Not logged in") || !s.relayError {
		t.Fatalf("%+v", s)
	}
	if bindings.Get("s1") != "" {
		t.Fatal("a failed start must not bind")
	}
}

func TestUnknownModelFailsBeforePrompting(t *testing.T) {
	f := newFake(t, "ok")
	rec := post(t, map[string]any{"stream": true, "model": "antigravity/nope", "session_id": "s1", "working_dir": t.TempDir(), "messages": user("hi")})
	s := summarize(t, rec.Body.String())
	if s.finish != "error" || !strings.Contains(s.text, `model "nope" is not available`) {
		t.Fatalf("%+v", s)
	}
	if len(f.params("session/prompt")) != 0 {
		t.Fatal("no prompt may run on the wrong model")
	}
}

func TestExitBeforeResultIsIncomplete(t *testing.T) {
	newFake(t, "exit")
	rec := post(t, map[string]any{"stream": true, "session_id": "s1", "working_dir": t.TempDir(), "messages": user("hi")})
	s := summarize(t, rec.Body.String())
	if s.finish != "error" || !strings.Contains(s.text, "incomplete_result") || s.relayError {
		t.Fatalf("%+v", s)
	}
}

func TestRestrictedModeRefusesPermissionRequests(t *testing.T) {
	f := newFake(t, "permission")
	post(t, map[string]any{"stream": true, "session_id": "s1", "working_dir": t.TempDir(), "permission_mode": "read-only", "messages": user("hi")})
	reqs := f.requests()
	if len(reqs[0].Argv) != 1 {
		t.Fatalf("a restricted mode must keep permission prompts on: argv=%v", reqs[0].Argv)
	}
	mode := ""
	for _, o := range f.params("session/set_config_option") {
		if o["configId"] == "mode" {
			mode = o["value"].(string)
		}
	}
	if mode != "plan" {
		t.Fatalf("mode=%q", mode)
	}
	answered := false
	for _, r := range reqs {
		if strings.Contains(string(r.Msg.ID), "p1") {
			answered = true
			if !strings.Contains(strings.ReplaceAll(string(r.Msg.Result), " ", ""), `"optionId":"deny"`) {
				t.Fatalf("answer=%s", r.Msg.Result)
			}
		}
	}
	if !answered {
		t.Fatal("the permission request was not answered")
	}
}

func TestClientDisconnectCancelsAndMetersTheTurn(t *testing.T) {
	f := newFake(t, "cancel")
	ctx, cancel := context.WithCancel(context.Background())
	data, _ := json.Marshal(map[string]any{"stream": true, "model": "antigravity/m", "session_id": "s1", "working_dir": t.TempDir(), "messages": user("hi")})
	rec := httptest.NewRecorder()
	done := make(chan struct{})
	before := stats.Snapshot().TotalRequests
	go func() {
		chatCompletionsHandler(rec, httptest.NewRequest("POST", "/v1/chat/completions", bytes.NewReader(data)).WithContext(ctx))
		close(done)
	}()
	deadline := time.Now().Add(10 * time.Second)
	for len(f.params("session/prompt")) == 0 && time.Now().Before(deadline) {
		time.Sleep(20 * time.Millisecond)
	}
	cancel()
	select {
	case <-done:
	case <-time.After(15 * time.Second):
		t.Fatal("the handler did not return after the client left")
	}
	if len(f.params("session/cancel")) != 1 {
		t.Fatal("the prompt must be cancelled through the protocol")
	}
	if stats.Snapshot().TotalRequests != before+1 {
		t.Fatal("the cancelled turn's tokens must still be metered")
	}
	if bindings.Get("s1") == "" {
		t.Fatal("the conversation stays bound after a cancel")
	}
}

func TestNonStreamReturnsText(t *testing.T) {
	newFake(t, "ok")
	rec := post(t, map[string]any{"session_id": "s1", "working_dir": t.TempDir(), "messages": user("hi")})
	var resp openai.ChatCompletionResponse
	if err := json.Unmarshal(rec.Body.Bytes(), &resp); err != nil {
		t.Fatal(err)
	}
	if rec.Code != 200 || resp.Choices[0].Message.ContentString() != "The directory contains a.txt." || resp.Usage == nil {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
}

func TestNonStreamFailureIsAnError(t *testing.T) {
	newFake(t, "auth")
	rec := post(t, map[string]any{"session_id": "s1", "working_dir": t.TempDir(), "messages": user("hi")})
	if rec.Code != 500 || !strings.Contains(rec.Body.String(), "Not logged in") {
		t.Fatalf("code=%d body=%s", rec.Code, rec.Body.String())
	}
}

func TestImagesGoAsNativeBlocksAndFilesAsPaths(t *testing.T) {
	f := newFake(t, "ok")
	work := t.TempDir()
	png := "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
	post(t, map[string]any{"stream": true, "session_id": "s1", "working_dir": work, "messages": []map[string]any{{"role": "user", "content": []map[string]any{
		{"type": "text", "text": "what is this"},
		{"type": "image_url", "image_url": map[string]string{"url": "data:image/png;base64," + png}},
	}}}})
	blocks := f.params("session/prompt")[0]["prompt"].([]any)
	if len(blocks) != 2 {
		t.Fatalf("blocks=%v", blocks)
	}
	image := blocks[1].(map[string]any)
	if image["type"] != "image" || image["mimeType"] != "image/png" || image["data"] != png {
		t.Fatalf("image block=%v", image)
	}
	text := blocks[0].(map[string]any)["text"].(string)
	if !strings.HasPrefix(text, "what is this") || !strings.Contains(text, "[Image file: "+filepath.Join(work, ".relay_uploads", "s1")) {
		t.Fatalf("text=%q", text)
	}
	if _, err := os.Stat(filepath.Join(work, ".relay_uploads", "s1")); !os.IsNotExist(err) {
		t.Fatal("staged uploads are removed after the turn")
	}
}

func TestParseModels(t *testing.T) {
	newFake(t, "ok")
	modelCache.models = nil
	got := listModels()
	want := []string{"antigravity/gemini-4-neon", "antigravity/gpt-oss-120b-medium"}
	if strings.Join(got, ",") != strings.Join(want, ",") {
		t.Fatalf("models=%v", got)
	}
	modelCache.models = nil
}

func TestStreamedImageLinkIsReassembledAcrossChunks(t *testing.T) {
	work := t.TempDir()
	png := []byte("\x89PNG\r\n\x1a\n0000000000000000")
	if err := os.WriteFile(filepath.Join(work, "chart.png"), png, 0600); err != nil {
		t.Fatal(err)
	}
	oldStore := sessionStore
	sessionStore = sessions.New(t.TempDir())
	t.Cleanup(func() { sessionStore = oldStore })
	var texts []string
	images := 0
	sink := &streamSink{images: newTurnImages(work), chunk: func(m *openai.ChatMessage) {
		if len(m.Images) > 0 {
			images++
			texts = append(texts, "<img>")
		} else {
			texts = append(texts, m.ContentString())
		}
	}}
	for _, piece := range []string{"See !", "[the chart](cha", "rt.png) and 5! done", " ![missing](nope.png)"} {
		sink.Text(piece)
	}
	sink.finish()
	if got := strings.Join(texts, ""); got != "See <img> and 5! done ![missing](nope.png)" || images != 1 {
		t.Fatalf("got %q images=%d", got, images)
	}
}
