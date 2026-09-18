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

func personalEnv(chat string) map[string]string {
	return map[string]string{"COREMAN_PLATFORM": "feishu", "COREMAN_CHAT_TYPE": chat, "COREMAN_FEISHU_PERSONAL_URL": "https://example.test/personal", "COREMAN_FEISHU_PERSONAL_TOKEN": "secret-marker"}
}

// withoutPersonal drops the `-c mcp_servers.coreman_feishu_personal.*`
// overrides, the only arguments the personal server touches.
func withoutPersonal(args []string) []string {
	var out []string
	for i := 0; i < len(args); i++ {
		if args[i] == "-c" && i+1 < len(args) && strings.HasPrefix(args[i+1], "mcp_servers.coreman_feishu_personal.") {
			i++
			continue
		}
		out = append(out, args[i])
	}
	return out
}

func TestPersonalConfigScopedPerTurn(t *testing.T) {
	for _, thread := range []string{"", "existing"} {
		for _, platform := range []string{"feishu", "wecom", ""} {
			for _, chat := range []string{"single", "cron", "group", ""} {
				for _, token := range []string{"secret-marker", ""} {
					env := personalEnv(chat)
					env["COREMAN_PLATFORM"] = platform
					env["COREMAN_FEISHU_PERSONAL_TOKEN"] = token
					req := &openai.ChatCompletionRequest{SessionID: "s", EnvVars: env}
					args := buildCodexInput(req, "", thread, "").Args
					joined := strings.Join(args, " ")
					allowed := platform == "feishu" && (chat == "single" || chat == "cron") && token != ""
					if allowed && (!strings.Contains(joined, "mcp_servers.coreman_feishu_personal.enabled=true") || !strings.Contains(joined, `mcp_servers.coreman_feishu_personal.url="https://example.test/personal"`)) {
						t.Fatalf("missing personal config: %s", joined)
					}
					if !allowed && !strings.Contains(joined, "mcp_servers.coreman_feishu_personal.enabled=false") {
						t.Fatalf("missing explicit personal deny: %s", joined)
					}
					if strings.Contains(joined, "--ephemeral") || strings.Contains(joined, "--disable memories") {
						t.Fatalf("personal tools must not drop the thread or memories: %s", joined)
					}
					if (thread != "") != strings.Contains(joined, "exec resume existing") {
						t.Fatalf("thread resume changed by personal tools: %s", joined)
					}
					if strings.Contains(joined, "secret-marker") {
						t.Fatal("token leaked into argv")
					}
				}
			}
		}
	}
}

func TestPersonalEnvironmentIsolation(t *testing.T) {
	t.Setenv("COREMAN_FEISHU_PERSONAL_TOKEN", "stale")
	t.Setenv("COREMAN_FEISHU_PERSONAL_URL", "https://stale.test")
	for _, env := range []map[string]string{nil, {"COREMAN_FEISHU_PERSONAL_TOKEN": "stale", "COREMAN_FEISHU_PERSONAL_URL": "https://stale.test", "COREMAN_PLATFORM": "feishu", "COREMAN_CHAT_TYPE": "group"}} {
		for _, entry := range cleanEnv(env) {
			if strings.HasPrefix(entry, "COREMAN_FEISHU_PERSONAL_") {
				t.Fatal("stale credential inherited")
			}
		}
	}
	found := false
	for _, entry := range cleanEnv(personalEnv("cron")) {
		found = found || entry == "COREMAN_FEISHU_PERSONAL_TOKEN=secret-marker"
	}
	if !found {
		t.Fatal("current task credential missing")
	}
}

// Personal tools are additive: the turn keeps the bot's sandbox, directories,
// rules and collaboration server.
func TestPersonalKeepsNormalCapabilities(t *testing.T) {
	ordinary := func() *openai.ChatCompletionRequest {
		return &openai.ChatCompletionRequest{
			SessionID: "s", PermissionMode: "workspace-write", AddDirs: []string{"/shared"}, Effort: "high",
			Messages: []openai.ChatMessage{{Role: "system", Content: json.RawMessage(`"rules"`)}, {Role: "user", Content: json.RawMessage(`"hi"`)}},
			EnvVars:  map[string]string{"COREMAN_PLATFORM": "feishu", "COREMAN_CHAT_TYPE": "single", "COREMAN_COLLABORATION_URL": "https://example.test/collab", "COREMAN_COLLABORATION_TOKEN": "collab-secret"},
		}
	}
	personal := ordinary()
	for k, v := range personalEnv("single") {
		personal.EnvVars[k] = v
	}
	args := buildCodexInput(personal, "m", "", "").Args
	joined := strings.Join(args, " ")
	for _, want := range []string{"mcp_servers.coreman_collaboration.enabled=true", "mcp_servers.coreman_feishu_personal.enabled=true", "-s workspace-write", "--add-dir /shared"} {
		if !strings.Contains(joined, want) {
			t.Errorf("missing %s: %s", want, joined)
		}
	}
	base := buildCodexInput(ordinary(), "m", "", "").Args
	if !reflect.DeepEqual(withoutPersonal(args), withoutPersonal(base)) {
		t.Fatalf("personal turn differs beyond its MCP server:\n%q\n%q", args, base)
	}
}

func TestHealthAdvertisesAdditivePersonalTools(t *testing.T) {
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
	if !body.Capabilities["feishu_personal_tools_v1"] || !body.Capabilities["owner_session_view_v1"] {
		t.Fatal("missing capability", recorder.Body.String())
	}
	if _, ok := body.Capabilities["feishu_personal_restricted_v1"]; ok {
		t.Fatal("retired restricted capability advertised", recorder.Body.String())
	}
}

// Personal turns resume their thread, so a missing rollout must still start a
// new thread even though their stderr stays out of the log.
func TestPersonalMissingRolloutStartsNewThread(t *testing.T) {
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
	handleStreamResponse(rec, httptest.NewRequest("POST", "/", nil), resumeInput(), "r", 1, "m", false, dir, personalEnv("single"), "s1", countingRebuild(&rebuilt))
	if body := rec.Body.String(); rebuilt != 1 || !strings.Contains(body, "hello again") {
		t.Fatalf("rebuilt=%d body:\n%s", rebuilt, body)
	}
	if strings.Contains(output.String(), "no rollout found") {
		t.Error("personal stderr reached the log")
	}
}
