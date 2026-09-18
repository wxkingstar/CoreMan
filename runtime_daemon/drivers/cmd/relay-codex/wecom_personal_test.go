package main

import (
	"bytes"
	"clawrelay-api/pkg/openai"
	"encoding/json"
	"log"
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

// withoutWecomPersonal drops the `-c mcp_servers.coreman_wecom_personal.*`
// overrides, the only arguments the WeCom personal server touches.
func withoutWecomPersonal(args []string) []string {
	var out []string
	for i := 0; i < len(args); i++ {
		if args[i] == "-c" && i+1 < len(args) && strings.HasPrefix(args[i+1], "mcp_servers.coreman_wecom_personal.") {
			i++
			continue
		}
		out = append(out, args[i])
	}
	return out
}

func TestWecomPersonalConfigScopedPerTurn(t *testing.T) {
	for _, thread := range []string{"", "existing"} {
		for _, platform := range []string{"wecom", "feishu", ""} {
			for _, chat := range []string{"single", "cron", "group", ""} {
				for _, missing := range []string{"", "COREMAN_WECOM_PERSONAL_URL", "COREMAN_WECOM_PERSONAL_TOKEN"} {
					env := wecomPersonalEnv(chat)
					env["COREMAN_PLATFORM"] = platform
					if missing != "" {
						env[missing] = " "
					}
					req := &openai.ChatCompletionRequest{SessionID: "s", EnvVars: env}
					args := buildCodexInput(req, "", thread, "").Args
					joined := strings.Join(args, " ")
					allowed := platform == "wecom" && (chat == "single" || chat == "cron") && missing == ""
					if allowed && (!strings.Contains(joined, "mcp_servers.coreman_wecom_personal.enabled=true") || !strings.Contains(joined, `mcp_servers.coreman_wecom_personal.url="https://example.test/wecom"`) || !strings.Contains(joined, `mcp_servers.coreman_wecom_personal.bearer_token_env_var="COREMAN_WECOM_PERSONAL_TOKEN"`)) {
						t.Fatalf("missing WeCom personal config: %s", joined)
					}
					if !allowed && (!strings.Contains(joined, "mcp_servers.coreman_wecom_personal.enabled=false") || !strings.Contains(joined, `mcp_servers.coreman_wecom_personal.url="http://127.0.0.1:1/disabled"`) || strings.Contains(joined, "coreman_wecom_personal.bearer_token_env_var")) {
						t.Fatalf("missing explicit WeCom personal deny: %s", joined)
					}
					// WeCom credentials never enable the Feishu server.
					if !strings.Contains(joined, "mcp_servers.coreman_feishu_personal.enabled=false") {
						t.Fatalf("Feishu personal server not disabled: %s", joined)
					}
					if strings.Contains(joined, "--ephemeral") || strings.Contains(joined, "--disable memories") {
						t.Fatalf("personal tools must not drop the thread or memories: %s", joined)
					}
					if (thread != "") != strings.Contains(joined, "exec resume existing") {
						t.Fatalf("thread resume changed by personal tools: %s", joined)
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
		{"COREMAN_WECOM_PERSONAL_TOKEN": "", "COREMAN_WECOM_PERSONAL_URL": "https://stale.test", "COREMAN_PLATFORM": "wecom", "COREMAN_CHAT_TYPE": "single"},
	} {
		for _, entry := range cleanEnv(env) {
			if strings.HasPrefix(entry, "COREMAN_WECOM_PERSONAL_") {
				t.Fatal("stale credential inherited", env)
			}
		}
	}
	found := 0
	for _, entry := range cleanEnv(wecomPersonalEnv("cron")) {
		if entry == "COREMAN_WECOM_PERSONAL_TOKEN=wecom-marker" || entry == "COREMAN_WECOM_PERSONAL_URL=https://example.test/wecom" {
			found++
		}
	}
	if found != 2 {
		t.Fatal("current task credential missing")
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
	count := func() (wecom, feishu int) {
		for _, entry := range cleanEnv(env) {
			if strings.HasPrefix(entry, "COREMAN_WECOM_PERSONAL_") {
				wecom++
			}
			if strings.HasPrefix(entry, "COREMAN_FEISHU_PERSONAL_") {
				feishu++
			}
		}
		return
	}
	if wecom, feishu := count(); wecom != 2 || feishu != 0 {
		t.Fatalf("WeCom turn forwarded wecom=%d feishu=%d personal vars", wecom, feishu)
	}
	env["COREMAN_PLATFORM"] = "feishu"
	if wecom, feishu := count(); wecom != 0 || feishu != 2 {
		t.Fatalf("Feishu turn forwarded wecom=%d feishu=%d personal vars", wecom, feishu)
	}
}

// WeCom personal tools are additive too: the turn keeps the bot's sandbox,
// directories, rules and collaboration server.
func TestWecomPersonalKeepsNormalCapabilities(t *testing.T) {
	ordinary := func() *openai.ChatCompletionRequest {
		return &openai.ChatCompletionRequest{
			SessionID: "s", PermissionMode: "workspace-write", AddDirs: []string{"/shared"}, Effort: "high",
			Messages: []openai.ChatMessage{{Role: "system", Content: json.RawMessage(`"rules"`)}, {Role: "user", Content: json.RawMessage(`"hi"`)}},
			EnvVars:  map[string]string{"COREMAN_PLATFORM": "wecom", "COREMAN_CHAT_TYPE": "single", "COREMAN_COLLABORATION_URL": "https://example.test/collab", "COREMAN_COLLABORATION_TOKEN": "collab-secret"},
		}
	}
	personal := ordinary()
	for k, v := range wecomPersonalEnv("single") {
		personal.EnvVars[k] = v
	}
	args := buildCodexInput(personal, "m", "", "").Args
	joined := strings.Join(args, " ")
	for _, want := range []string{"mcp_servers.coreman_collaboration.enabled=true", "mcp_servers.coreman_wecom_personal.enabled=true", "mcp_servers.coreman_feishu_personal.enabled=false", "-s workspace-write", "--add-dir /shared"} {
		if !strings.Contains(joined, want) {
			t.Errorf("missing %s: %s", want, joined)
		}
	}
	base := buildCodexInput(ordinary(), "m", "", "").Args
	if !reflect.DeepEqual(withoutWecomPersonal(args), withoutWecomPersonal(base)) {
		t.Fatalf("personal turn differs beyond its MCP server:\n%q\n%q", args, base)
	}
}

func TestHealthAdvertisesWecomPersonalTools(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, "codex"), []byte("#!/bin/sh\necho 'codex 1.0'\n"), 0700); err != nil {
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

// WeCom personal turns resume their thread, so a missing rollout must still
// start a new thread even though their stderr stays out of the log.
func TestWecomPersonalMissingRolloutStartsNewThread(t *testing.T) {
	dir := fakeCodex(t, missingRolloutOnResume)
	withSessionState(t)
	if err := threads.Set("s1", "dead-thread"); err != nil {
		t.Fatal(err)
	}
	var output bytes.Buffer
	old := log.Writer()
	log.SetOutput(&output)
	defer log.SetOutput(old)
	rebuilt := 0
	rec := httptest.NewRecorder()
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), resumeInput(), "r", 1, "m", false, dir, wecomPersonalEnv("single"), "s1", countingRebuild(&rebuilt))
	if body := rec.Body.String(); rebuilt != 1 || !strings.Contains(body, "hello again") {
		t.Fatalf("rebuilt=%d body:\n%s", rebuilt, body)
	}
	if strings.Contains(output.String(), "no rollout found") {
		t.Error("personal stderr reached the log")
	}
}

// privateOutputCodex answers with a marker in a JSONL event, a non-JSON line,
// a malformed JSON line and on stderr.
const privateOutputCodex = `/bin/cat >/dev/null
echo 'PRIVATE-STDERR-MARKER' >&2
echo 'PRIVATE-BANNER-MARKER'
echo '{PRIVATE-PARSE-MARKER'
echo '{"type":"thread.started","thread_id":"t1"}'
echo '{"type":"item.completed","item":{"type":"agent_message","text":"PRIVATE-OUTPUT-MARKER"}}'
echo '{"type":"turn.completed"}'
`

// Even under RELAY_DEBUG=1, a WeCom personal turn keeps model output and CLI
// diagnostics out of the log on both response paths, as a Feishu one does.
func TestWecomPersonalDebugLogsOmitPrivateContent(t *testing.T) {
	dir := fakeCodex(t, privateOutputCodex)
	t.Setenv("RELAY_DEBUG", "1")
	input := func() codexInput { return codexInput{Args: []string{"exec", "--json", "-"}} }
	handlers := map[string]func(rec *httptest.ResponseRecorder, env map[string]string){
		"stream": func(rec *httptest.ResponseRecorder, env map[string]string) {
			handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), input(), "r", 1, "m", false, dir, env, "s1", input)
		},
		"non-stream": func(rec *httptest.ResponseRecorder, env map[string]string) {
			handleNonStreamResponse(rec, httptest.NewRequest("POST", "/", nil), input(), "r", 1, "m", dir, env, "s1", input)
		},
	}
	markers := []string{"PRIVATE-STDERR-MARKER", "PRIVATE-BANNER-MARKER", "PRIVATE-PARSE-MARKER", "PRIVATE-OUTPUT-MARKER"}
	for name, handle := range handlers {
		for kind, env := range map[string]map[string]string{"ordinary": {"COREMAN_PLATFORM": "wecom", "COREMAN_CHAT_TYPE": "single"}, "wecom": wecomPersonalEnv("single"), "feishu": personalEnv("single")} {
			t.Run(name+"/"+kind, func(t *testing.T) {
				withSessionState(t)
				var output bytes.Buffer
				old := log.Writer()
				log.SetOutput(&output)
				defer log.SetOutput(old)
				rec := httptest.NewRecorder()
				handle(rec, env)
				if !strings.Contains(rec.Body.String(), "PRIVATE-OUTPUT-MARKER") {
					t.Fatalf("answer missing from response: %s", rec.Body.String())
				}
				logs := output.String()
				for _, marker := range markers {
					leaked := strings.Contains(logs, marker)
					if kind == "ordinary" && !leaked {
						t.Fatalf("control: debug logs should show ordinary %s:\n%s", marker, logs)
					}
					if kind != "ordinary" && leaked {
						t.Fatalf("debug logs leaked personal %s:\n%s", marker, logs)
					}
				}
			})
		}
	}
}
