package main

import (
	"bytes"
	"clawrelay-api/pkg/openai"
	"clawrelay-api/pkg/sessions"
	"encoding/json"
	"log"
	"net/http"
	"net/http/httptest"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func wecomPersonalEnv(chat string) map[string]string {
	return map[string]string{"COREMAN_PLATFORM": "wecom", "COREMAN_CHAT_TYPE": chat, "COREMAN_WECOM_PERSONAL_URL": "https://example.test/wecom", "COREMAN_WECOM_PERSONAL_TOKEN": "wecom-marker"}
}

func TestWecomPersonalConfigScopedPerTurn(t *testing.T) {
	for _, session := range []string{"", "existing"} {
		for _, platform := range []string{"wecom", "feishu", ""} {
			for _, chat := range []string{"single", "cron", "group", ""} {
				for _, missing := range []string{"", "COREMAN_WECOM_PERSONAL_URL", "COREMAN_WECOM_PERSONAL_TOKEN"} {
					env := wecomPersonalEnv(chat)
					env["COREMAN_PLATFORM"] = platform
					if missing != "" {
						env[missing] = " "
					}
					req := &openai.ChatCompletionRequest{SessionID: session, EnvVars: env}
					args, _ := buildClaudeArgs(req, "", nil, "")
					joined := strings.Join(args, " ")
					allowed := platform == "wecom" && (chat == "single" || chat == "cron") && missing == ""
					servers := mcpServers(t, args)
					server, mounted := servers["coreman_wecom_personal"]
					if mounted != allowed || strings.Contains(joined, "mcp__coreman_wecom_personal") == allowed {
						t.Fatalf("platform=%q chat=%q missing=%q: mounted=%v: %s", platform, chat, missing, mounted, joined)
					}
					if allowed && (server["url"] != "https://example.test/wecom" || !strings.Contains(joined, "Bearer ${COREMAN_WECOM_PERSONAL_TOKEN}")) {
						t.Fatalf("WeCom personal server misconfigured: %s", joined)
					}
					// WeCom credentials never mount the Feishu server.
					if _, ok := servers["coreman_feishu_personal"]; ok || !strings.Contains(joined, "mcp__coreman_feishu_personal") {
						t.Fatalf("Feishu personal server not denied: %s", joined)
					}
					if (session != "") != strings.Contains(joined, "--resume "+session) {
						t.Fatalf("session resume changed by personal tools: %s", joined)
					}
					if strings.Contains(joined, "wecom-marker") {
						t.Fatal("token leaked into argv")
					}
				}
			}
		}
	}
}

func TestWecomPersonalEnvironmentIsolation(t *testing.T) {
	t.Setenv("COREMAN_WECOM_PERSONAL_TOKEN", "stale")
	t.Setenv("COREMAN_WECOM_PERSONAL_URL", "https://stale.test")
	for _, env := range []map[string]string{
		nil,
		{"COREMAN_WECOM_PERSONAL_TOKEN": "stale", "COREMAN_WECOM_PERSONAL_URL": "https://stale.test", "COREMAN_PLATFORM": "wecom", "COREMAN_CHAT_TYPE": "group"},
		{"COREMAN_WECOM_PERSONAL_TOKEN": "stale", "COREMAN_WECOM_PERSONAL_URL": "https://stale.test", "COREMAN_PLATFORM": "feishu", "COREMAN_CHAT_TYPE": "single"},
		{"COREMAN_WECOM_PERSONAL_TOKEN": "stale", "COREMAN_WECOM_PERSONAL_URL": "", "COREMAN_PLATFORM": "wecom", "COREMAN_CHAT_TYPE": "single"},
	} {
		for _, entry := range cleanEnv(env) {
			if strings.HasPrefix(entry, "COREMAN_WECOM_PERSONAL_") {
				t.Fatal("stale credential inherited", env)
			}
		}
	}
	var got []string
	for _, entry := range cleanEnv(wecomPersonalEnv("cron")) {
		if strings.HasPrefix(entry, "COREMAN_WECOM_PERSONAL_") {
			got = append(got, entry)
		}
	}
	if len(got) != 2 || !strings.Contains(strings.Join(got, " "), "COREMAN_WECOM_PERSONAL_TOKEN=wecom-marker") {
		t.Fatal("current task credential missing", got)
	}
}

// A turn mounts at most its own platform's personal server, so the other
// platform's credentials never reach the CLI.
func TestPersonalCredentialsStayWithTheirServer(t *testing.T) {
	env := wecomPersonalEnv("single")
	for k, v := range personalEnv("single") {
		if k != "COREMAN_PLATFORM" {
			env[k] = v
		}
	}
	var wecom, feishu int
	for _, entry := range cleanEnv(env) {
		if strings.HasPrefix(entry, "COREMAN_WECOM_PERSONAL_") {
			wecom++
		}
		if strings.HasPrefix(entry, "COREMAN_FEISHU_PERSONAL_") {
			feishu++
		}
	}
	if wecom != 2 || feishu != 0 {
		t.Fatalf("WeCom turn forwarded wecom=%d feishu=%d personal vars", wecom, feishu)
	}
	env["COREMAN_PLATFORM"] = "feishu"
	wecom, feishu = 0, 0
	for _, entry := range cleanEnv(env) {
		if strings.HasPrefix(entry, "COREMAN_WECOM_PERSONAL_") {
			wecom++
		}
		if strings.HasPrefix(entry, "COREMAN_FEISHU_PERSONAL_") {
			feishu++
		}
	}
	if wecom != 0 || feishu != 2 {
		t.Fatalf("Feishu turn forwarded wecom=%d feishu=%d personal vars", wecom, feishu)
	}
}

// WeCom personal tools are additive too: the turn keeps the bot's prompt file,
// tools, settings, directories, permission mode and session.
func TestWecomPersonalKeepsNormalCapabilities(t *testing.T) {
	ordinary := func() *openai.ChatCompletionRequest {
		return &openai.ChatCompletionRequest{SessionID: "old", SystemPromptFile: "/shared/prompt", Settings: `{"hooks":{}}`, AllowedTools: "Bash", PermissionMode: "acceptEdits", AddDirs: []string{"/shared"}, EnvVars: map[string]string{"COREMAN_PLATFORM": "wecom", "COREMAN_CHAT_TYPE": "single"}}
	}
	personal := ordinary()
	for k, v := range wecomPersonalEnv("single") {
		personal.EnvVars[k] = v
	}
	args, _ := buildClaudeArgs(personal, "m", textPrompt("hi"), "rules")
	if _, ok := mcpServers(t, args)["coreman_wecom_personal"]; !ok {
		t.Fatal("WeCom personal server not mounted", args)
	}
	joined := strings.Join(args, " ")
	for _, required := range []string{"--resume old", "--append-system-prompt-file /shared/prompt", `--settings {"hooks":{}}`, "--allowedTools Bash", "--add-dir /shared", "--permission-mode acceptEdits", "--append-system-prompt rules"} {
		if !strings.Contains(joined, required) {
			t.Error("missing normal capability", required)
		}
	}
	for _, forbidden := range []string{"--restricted", "--tools", "--strict-mcp-config", "--no-session-persistence", "--disable-slash-commands", "disableAllHooks"} {
		if strings.Contains(joined, forbidden) {
			t.Error("restricted flag", forbidden)
		}
	}
	base, _ := buildClaudeArgs(ordinary(), "m", textPrompt("hi"), "rules")
	if !reflect.DeepEqual(withoutMCP(args), withoutMCP(base)) {
		t.Fatalf("personal turn differs beyond MCP:\n%q\n%q", args, base)
	}
}

func TestWecomPersonalMountsAlongsideCollaboration(t *testing.T) {
	env := wecomPersonalEnv("single")
	env["COREMAN_COLLABORATION_URL"] = "https://example.test/collab"
	env["COREMAN_COLLABORATION_TOKEN"] = "collab-secret"
	args, _ := buildClaudeArgs(&openai.ChatCompletionRequest{EnvVars: env}, "", nil, "")
	servers := mcpServers(t, args)
	if servers["coreman_collaboration"]["url"] != "https://example.test/collab" || servers["coreman_wecom_personal"]["url"] != "https://example.test/wecom" {
		t.Fatal("both MCP servers must be mounted", args)
	}
	// Only the Feishu server, which this WeCom turn cannot mount, is denied.
	for i, a := range args {
		if a == "--disallowedTools" && args[i+1] != "mcp__coreman_feishu_personal" {
			t.Fatal("mounted server denied", args)
		}
	}
}

// WeCom personal turns resume their session, so a missing session must still
// be recreated even though their stderr stays out of the log.
func TestWecomPersonalResumeRetriesWhenSessionMissing(t *testing.T) {
	writeFakeClaude(t, `echo "$*" >> "$ARGSLOG"
case "$*" in
*--session-id*)
  echo '{"type":"system","subtype":"init"}'
  echo '{"type":"result","subtype":"success","result":"hello"}'
  ;;
*--resume*)
  echo "No conversation found with session ID: sess-x" >&2
  exit 1
  ;;
esac
`)
	_, env, count := newArgsLog(t)
	for k, v := range wecomPersonalEnv("single") {
		env[k] = v
	}
	var output bytes.Buffer
	old := log.Writer()
	log.SetOutput(&output)
	defer log.SetOutput(old)

	lines, ready, _ := startClaudeStream([]string{"--resume", "sess-x"}, "hi", "", env)
	if err := waitReadyErr(t, ready); err != nil {
		t.Fatalf("ready returned error, want nil: %v", err)
	}
	if got := strings.Join(collectAllLines(t, lines), "\n"); !strings.Contains(got, `"result":"hello"`) {
		t.Errorf("retry output was not forwarded; got: %s", got)
	}
	if n := count(); n != 2 {
		t.Errorf("claude invoked %d times, want 2 (resume + --session-id retry)", n)
	}
	if strings.Contains(output.String(), "No conversation found") {
		t.Error("personal stderr reached the log")
	}
}

// privateOutputClaude answers with a marker on stdout (as a streamed delta,
// an assistant message and the result) and on stderr.
const privateOutputClaude = `/bin/cat >/dev/null
echo 'PRIVATE-STDERR-MARKER' >&2
echo '{"type":"system","subtype":"init"}'
echo '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"PRIVATE-OUTPUT-MARKER"}}}'
echo '{"type":"assistant","message":{"content":[{"type":"text","text":"PRIVATE-OUTPUT-MARKER"}]}}'
echo '{"type":"result","subtype":"success","result":"PRIVATE-OUTPUT-MARKER"}'
`

// Even under RELAY_DEBUG=1, a WeCom personal turn keeps model output and CLI
// stderr out of the log on every response path, as a Feishu one does.
func TestWecomPersonalDebugLogsOmitPrivateContent(t *testing.T) {
	writeFakeClaude(t, privateOutputClaude)
	t.Setenv("RELAY_DEBUG", "1")
	handlers := map[string]func(w http.ResponseWriter, r *http.Request, env map[string]string){
		"stream": func(w http.ResponseWriter, r *http.Request, env map[string]string) {
			handleStreamResponse(w, r, []string{"--model", "m"}, "hi", "chat-p", 1, "m", false, "", env, "")
		},
		"buffered": func(w http.ResponseWriter, r *http.Request, env map[string]string) {
			handleBufferedStreamResponse(w, r, []string{"--model", "m"}, "hi", "chat-p", 1, "m", false, "", env, "")
		},
		"non-stream": func(w http.ResponseWriter, r *http.Request, env map[string]string) {
			handleNonStreamResponse(w, r, []string{"--model", "m"}, "hi", "chat-p", 1, "m", false, "", env, "")
		},
	}
	for name, handle := range handlers {
		for kind, env := range map[string]map[string]string{"ordinary": {"COREMAN_PLATFORM": "wecom", "COREMAN_CHAT_TYPE": "single"}, "wecom": wecomPersonalEnv("single"), "feishu": personalEnv("single")} {
			t.Run(name+"/"+kind, func(t *testing.T) {
				sessionStore = sessions.New(t.TempDir())
				var output bytes.Buffer
				old := log.Writer()
				log.SetOutput(&output)
				defer log.SetOutput(old)
				srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) { handle(w, r, env) }))
				resp, err := http.Get(srv.URL)
				if err != nil {
					srv.Close()
					t.Fatal(err)
				}
				var body bytes.Buffer
				body.ReadFrom(resp.Body) //nolint:errcheck // checked via contents below
				resp.Body.Close()
				srv.Close()
				if !strings.Contains(body.String(), "PRIVATE-OUTPUT-MARKER") {
					t.Fatalf("answer missing from response: %s", body.String())
				}
				logs := output.String()
				leaked := strings.Contains(logs, "PRIVATE-OUTPUT-MARKER") || strings.Contains(logs, "PRIVATE-STDERR-MARKER")
				if kind == "ordinary" && !leaked {
					t.Fatalf("control: debug logs should show ordinary content:\n%s", logs)
				}
				if kind != "ordinary" && leaked {
					t.Fatalf("debug logs leaked personal content:\n%s", logs)
				}
			})
		}
	}
}

func TestHealthAdvertisesWecomPersonalTools(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "claude"), []byte("#!/bin/sh\necho 'claude 1.0'\n"), 0700); err != nil {
		t.Fatal(err)
	}
	t.Setenv("PATH", dir)
	recorder := httptest.NewRecorder()
	healthHandler(recorder, httptest.NewRequest("GET", "/health", nil))
	var body struct {
		Capabilities map[string]bool `json:"capabilities"`
	}
	if err := json.Unmarshal(recorder.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	for _, capability := range []string{"wecom_personal_tools_v1", "feishu_personal_tools_v1", "owner_session_view_v1"} {
		if !body.Capabilities[capability] {
			t.Fatal("missing capability", capability, recorder.Body.String())
		}
	}
}
