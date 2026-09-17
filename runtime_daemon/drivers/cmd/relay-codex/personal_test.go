package main

import (
	"clawrelay-api/pkg/openai"
	"strings"
	"testing"
)

func TestPersonalConfigScopedPerTurn(t *testing.T) {
	for _, session := range []string{"", "existing"} {
		for _, platform := range []string{"feishu", "wecom", ""} {
			for _, chat := range []string{"single", "group", ""} {
				for _, token := range []string{"secret-marker", ""} {
					req := &openai.ChatCompletionRequest{SessionID: session, EnvVars: map[string]string{"COREMAN_PLATFORM": platform, "COREMAN_CHAT_TYPE": chat, "COREMAN_FEISHU_PERSONAL_URL": "https://example.test/personal", "COREMAN_FEISHU_PERSONAL_TOKEN": token}}
					args := buildCodexInput(req, "", session, "").Args
					joined := strings.Join(args, " ")
					allowed := platform == "feishu" && chat == "single" && token != ""
					if allowed && (!strings.Contains(joined, "mcp_servers.coreman_feishu_personal.enabled=true") || !strings.Contains(joined, "--ephemeral --disable memories")) {
						t.Fatalf("missing personal config/privacy: %s", joined)
					}
					if !allowed && !strings.Contains(joined, "mcp_servers.coreman_feishu_personal.enabled=false") {
						t.Fatalf("missing explicit personal deny: %s", joined)
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
}
